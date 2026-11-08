"""Compare the timed runner with unittest on isolated test modules."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
from typing import ClassVar, cast, override
import unittest


from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message.process_test_support import OwnedTestProcesses
from scripts.test_times.gate_record import ModuleTimes
from scripts.test_times.module_run import FixtureMeasure, TestMeasure

RUNNER = Path(__file__).with_name("module_run.py").resolve()

OUTCOMES = '''\
import os
import unittest
import warnings
from fixture_root import VALUE

class Outcomes(unittest.TestCase):
    def test_pass(self) -> None:
        self.assertEqual(VALUE, 42)
        if os.environ.get("CHECK_REMOVED"):
            self.assertNotIn("TEST_TIMES_OUT", os.environ)

    def test_fail(self) -> None:
        self.fail("fixture failure")

    def test_error(self) -> None:
        raise RuntimeError("fixture error")

    @unittest.skip("fixture skip")
    def test_skip(self) -> None:
        self.fail("A skipped test must not run")

    @unittest.expectedFailure
    def test_expected_failure(self) -> None:
        self.fail("expected fixture failure")

    @unittest.expectedFailure
    def test_unexpected_success(self) -> None:
        pass

    def test_subtest_fail(self) -> None:
        with self.subTest(part="fail"):
            self.fail("subtest failure")
        with self.subTest(part="pass"):
            pass

    def test_subtest_error(self) -> None:
        with self.subTest(part="error"):
            raise ValueError("subtest error")
        with self.subTest(part="fail"):
            self.fail("second subtest failure")
        with self.subTest(part="skip"):
            self.skipTest("subtest skip")
        with self.subTest(part="pass"):
            pass

    def test_warning(self) -> None:
        warnings.warn("fixture warning", DeprecationWarning)

class BrokenClass(unittest.TestCase):
    @classmethod
    def setUpClass(cls: type["BrokenClass"]) -> None:
        raise RuntimeError("fixture class error")

    def test_unreached(self) -> None:
        pass
'''

SUCCESS = '''\
import unittest
from fixture_root import VALUE

class RootImport(unittest.TestCase):
    def test_value(self) -> None:
        self.assertEqual(VALUE, 42)
'''

DISCOVERY_BOUNDARIES = '''\
import os
import unittest

IMPORTED_OUTPUT = os.environ.pop("TEST_TIMES_OUT", None)

class DiscoveryBoundaries(unittest.TestCase):
    test_case: None = None

    def test_failure_with_test_case_attribute(self) -> None:
        self.fail("fixture attribute failure")

    def test_import_environment(self) -> None:
        self.assertIsNone(IMPORTED_OUTPUT, "Discovery must not receive the output path")
'''


ORPHAN_PROCESS = '''\
import os
import select
import signal
import sys
import unittest

class OrphanProcess(unittest.TestCase):
    def test_orphan(self):
        reader, writer = os.pipe()
        intermediate = os.fork()
        if intermediate == 0:
            os.close(reader)
            if os.fork() != 0:
                os._exit(0)
            os.setsid()
            os.write(writer, str(os.getpid()).encode())
            os.close(writer)
            for descriptor in (0, 1, 2):
                os.close(descriptor)
            while True:
                signal.pause()
        os.close(writer)
        pid = int(os.read(reader, 32))
        os.close(reader)
        os.waitpid(intermediate, 0)
        descriptor = os.pidfd_open(pid)
        try:
            print(pid, flush=True)
            sys.stdin.buffer.read(1)
            if __REAP__:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                reaped = select.poll()
                reaped.register(descriptor, select.POLLHUP)
                self.assertTrue(reaped.poll(__HANG_GUARD_MS__), "The orphan remains unreaped")
            if __FAIL__:
                self.fail("fixture failure after orphan launch")
        finally:
            os.close(descriptor)
'''


SENDER_REQUEST = '''\
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, __REPOSITORY__)
from scripts.message import session_send
from scripts.message import session_send_test_support

def setUpModule():
    session_send_test_support.forbid_real_sender()
    sys.addaudithook(last_guard)

real_sender = Path(session_send.__file__).resolve().with_name("send.py")
python = str(real_sender.parents[1] / "lib/py")

# This safety hook permits only harmless probes and blocks network delivery.
def last_guard(event, arguments):
    if event in ("socket.connect", "socket.sendto", "socket.bind"):
        raise RuntimeError("the sender probe attempted network access")
    if event in ("subprocess.Popen", "os.posix_spawn", "os.exec"):
        command = arguments[1]
    elif event == "os.spawn":
        command = arguments[2]
    elif event == "os.system":
        command = arguments[0]
    else:
        return
    if str(Path(__file__).with_name("fake_sender.py")) not in str(command) and "--help" not in str(command):
        raise RuntimeError("the sender probe attempted a live command")


MODE = __MODE__

class SenderRequest(unittest.TestCase):
    def test_sender(self):
        if MODE == "fake":
            fake = Path(__file__).with_name("fake_sender.py")
            fake.write_text("print('fake sender ran')\\n")
            with mock.patch.object(session_send, "SEND_SCRIPT", fake):
                result = subprocess.run(session_send.session_send_argv("fixture"),
                                        capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, "fake sender ran\\n")
        elif MODE == "cleared_environment":
            child_environment = os.environ.copy()
            marker = session_send_test_support.refusal_marker
            # This isolated process keeps its environment empty through runner completion.
            mock.patch.dict(os.environ, {}, clear=True).start()
            session_send_test_support.forbid_real_sender()
            self.assertIs(session_send_test_support.refusal_marker, marker)
            result = subprocess.run([python, str(real_sender), "--help"],
                                    env=child_environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.environ, {})
            self.assertIsInstance(marker, session_send_test_support.SenderRefusalMarker)
            self.assertEqual(marker.path.read_text(), "real sender refused\\n")
        elif MODE == "caught":
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run([python, str(real_sender), "--help"], check=True)
        else:
            if MODE == "cd":
                command = f"cd {shlex.quote(str(real_sender.parent))} && {shlex.quote(python)} send.py --help"
                directory = None
            elif MODE == "module":
                command = [python, "-m", "scripts.message.send", "--help"]
                directory = real_sender.parents[2]
            else:
                command = [python, str(real_sender), "--help"]
                directory = None
            result = subprocess.run(command, cwd=directory, shell=MODE == "cd", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

if MODE == "expected_failure":
    SenderRequest.test_sender = unittest.expectedFailure(SenderRequest.test_sender)
'''


@dataclass(frozen=True)
class FixtureRun:
    timed: subprocess.CompletedProcess[str]
    standard: subprocess.CompletedProcess[str]
    times: ModuleTimes


def read_times(path: Path) -> ModuleTimes:
    """Read output without using the product's reader."""
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise AssertionError("The runner must write a JSON object")
    return cast(ModuleTimes, cast(object, value))


def normalized_output(text: str) -> str:
    return re.sub(r"Ran (\d+) tests? in [0-9.]+s", r"Ran \1 tests in <time>s", text)


def run_fixture(root: Path, name: str, source: str) -> FixtureRun:
    folder = root / name
    folder.mkdir()
    _ = (folder / "test_fixture.py").write_text(textwrap.dedent(source), encoding="utf-8")
    output = root / f"{name}.json"
    _ = output.write_text("stale output\n", encoding="utf-8")
    env = dict(os.environ)
    for key in ("TEST_TIMES_OUT", "PYTHONPATH", "PYTHONWARNINGS"):
        _ = env.pop(key, None)
    env.update({"HOME": str(root / "home"), "BUILDLOG_DIR": str(root / "buildlog"),
                "CONF_DIR": str(root / "config"), "PYTHONDONTWRITEBYTECODE": "1"})
    args = ["discover", "-s", str(folder), "-p", "test_fixture.py"]
    standard = subprocess.run(
        [sys.executable, "-m", "unittest", *args], cwd=root, env=env,
        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
    )
    timed = subprocess.run(
        [sys.executable, str(RUNNER), str(folder), "test_fixture.py"], cwd=root,
        env={**env, "TEST_TIMES_OUT": str(output), "CHECK_REMOVED": "1"},
        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
    )
    return FixtureRun(timed, standard, read_times(output))


class ModuleRunnerTests(unittest.TestCase):
    outcomes: ClassVar[FixtureRun]
    empty: ClassVar[FixtureRun]
    success: ClassVar[FixtureRun]
    discovery_result: ClassVar[subprocess.CompletedProcess[str]]
    discovery_output: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        _ = (root / "fixture_root.py").write_text("VALUE = 42\n", encoding="utf-8")
        cls.outcomes = run_fixture(root, "outcomes", OUTCOMES)
        cls.empty = run_fixture(root, "empty", "# This module has no tests.\n")
        cls.success = run_fixture(root, "success", SUCCESS)
        folder = root / "discovery_boundaries"
        folder.mkdir()
        _ = (folder / "test_boundaries.py").write_text(DISCOVERY_BOUNDARIES, encoding="utf-8")
        cls.discovery_output = root / "discovery.json"
        env = {**os.environ, "TEST_TIMES_OUT": str(cls.discovery_output),
               "HOME": str(root / "home"), "CONF_DIR": str(root / "config"),
               "BUILDLOG_DIR": str(root / "buildlog"), "PYTHONDONTWRITEBYTECODE": "1"}
        _ = env.pop("PYTHONPATH", None)
        cls.discovery_result = subprocess.run(
            [sys.executable, str(RUNNER), str(folder), "test_boundaries.py"], cwd=root, env=env,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def sender_fixture(self, mode: str) -> FixtureRun:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.mkdir()
            _ = (config / "lint.conf").write_text("# range: 1..\nsweep_sender_timeout_seconds=17\n")
            source = SENDER_REQUEST.replace("__REPOSITORY__", repr(str(RUNNER.parents[2])))
            return run_fixture(root, "sender", source.replace("__MODE__", repr(mode)))

    def test_real_sender_request_fails_the_module_with_one_diagnostic(self) -> None:
        fixture = self.sender_fixture("uncaught")

        self.assertNotEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
        self.assertNotEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
        diagnostic = "test_fixture.py: started the real scripts/message/send.py"
        self.assertEqual(fixture.timed.stderr.splitlines().count(diagnostic), 1, fixture.timed.stderr)
        self.assertNotIn("the tested guard allowed the real sender", fixture.timed.stderr)
        self.assertEqual(fixture.times["tests_run"], 1)
        self.assertEqual(fixture.times["failures"], 1)

    def test_sender_from_its_folder_or_as_a_module_fails_with_one_diagnostic(self) -> None:
        for mode in ("cd", "module"):
            with self.subTest(mode=mode):
                fixture = self.sender_fixture(mode)
                self.assertNotEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
                diagnostic = "test_fixture.py: started the real scripts/message/send.py"
                self.assertEqual(fixture.timed.stderr.splitlines().count(diagnostic), 1, fixture.timed.stderr)
                self.assertIn("REFUSED: real scripts/message/send.py is forbidden in tests", fixture.timed.stderr)
                self.assertNotIn("the sender probe attempted", fixture.timed.stderr)

    def test_caught_sender_refusal_still_fails_the_module(self) -> None:
        for mode in ("caught", "expected_failure"):
            with self.subTest(mode=mode):
                fixture = self.sender_fixture(mode)

                self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
                self.assertNotEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
                diagnostic = "test_fixture.py: started the real scripts/message/send.py"
                self.assertEqual(fixture.timed.stderr.splitlines().count(diagnostic), 1, fixture.timed.stderr)
                self.assertNotIn("the tested guard allowed the real sender", fixture.timed.stderr)
                self.assertEqual(fixture.times["tests_run"], 1)
                self.assertEqual(fixture.times["failures"], 0)
                self.assertEqual(fixture.times["errors"], 0)
                expected_status = "expected_failure" if mode == "expected_failure" else "pass"
                self.assertEqual(fixture.times["tests"][0]["status"], expected_status)

    def test_cleared_environment_keeps_the_refusal_marker_and_fails_the_module(self) -> None:
        fixture = self.sender_fixture("cleared_environment")

        self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
        self.assertEqual(fixture.times["tests_run"], 1)
        self.assertEqual(fixture.times["tests"][0]["status"], "pass")
        self.assertNotEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
        diagnostic = "test_fixture.py: started the real scripts/message/send.py"
        self.assertEqual(fixture.timed.stderr.splitlines().count(diagnostic), 1, fixture.timed.stderr)
        self.assertNotIn("the sender probe attempted", fixture.timed.stderr)

    def test_fake_sender_module_passes_without_a_refusal_diagnostic(self) -> None:
        fixture = self.sender_fixture("fake")

        self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
        self.assertEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
        self.assertNotIn("started the real scripts/message/send.py", fixture.timed.stderr)
        self.assertEqual(fixture.times["tests_run"], 1)
        self.assertEqual(fixture.times["tests"][0]["status"], "pass")

    def test_normal_test_case_attribute_keeps_the_failure_entry(self) -> None:
        self.assertEqual(self.discovery_result.returncode, 1, self.discovery_result.stderr)
        self.assertTrue(self.discovery_output.is_file(), self.discovery_result.stderr)
        rows = read_times(self.discovery_output)["tests"]
        failures = [row for row in rows if row["test"] == "DiscoveryBoundaries.test_failure_with_test_case_attribute"]
        self.assertEqual(len(failures), 1, rows)
        self.assertEqual(failures[0]["status"], "fail")

    def test_discovery_import_does_not_receive_the_output_path(self) -> None:
        self.assertTrue(self.discovery_output.is_file(), self.discovery_result.stderr)
        times = read_times(self.discovery_output)
        imported = [row for row in times["tests"] if row["test"] == "DiscoveryBoundaries.test_import_environment"]
        self.assertEqual(len(imported), 1, times)
        self.assertEqual(imported[0]["status"], "pass")
        self.assertEqual(times["tests_run"], 2)
        self.assertEqual(times["failures"], 1)

    def test_none_fixtures_keep_the_standard_runner_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = run_fixture(Path(directory), "none_fixtures", """import unittest
setUpModule = None
tearDownModule = None
class Fixtures(unittest.TestCase):
    setUpClass = None
    tearDownClass = None
    def test_runs(self):
        pass
""")
        self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
        self.assertEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
        self.assertEqual(normalized_output(fixture.timed.stderr), normalized_output(fixture.standard.stderr))
        self.assertEqual(fixture.times["tests_run"], 1)

    def test_a_class_without_an_imported_module_keeps_the_standard_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = run_fixture(Path(directory), "absent_module", """import unittest
class Fixtures(unittest.TestCase):
    __module__ = "absent_module"
    def test_runs(self):
        pass
""")
        self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
        self.assertEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
        self.assertEqual(normalized_output(fixture.timed.stderr), normalized_output(fixture.standard.stderr))
        self.assertEqual(fixture.times["tests_run"], 1)

    def test_successful_class_fixture_has_its_own_cpu_measure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = """import time
import unittest

module_value = 0

def setUpModule():
    global module_value
    module_value = 7

class FixtureCPU(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        deadline = time.process_time() + 0.15
        while time.process_time() < deadline:
            pass
        cls.value = module_value

    def test_value(self):
        self.assertEqual(self.value, 7)
"""
            fixture = run_fixture(root, "fixture_cpu", source)
            self.assertEqual(fixture.timed.returncode, 0, fixture.timed.stderr)
            self.assertEqual(fixture.standard.returncode, 0, fixture.standard.stderr)
            report = cast(dict[str, object], cast(object, fixture.times))
            fixtures = cast(list[FixtureMeasure], report["fixtures"])
            setup = next(row for row in fixtures if row["test"] == "setUpClass (test_fixture.FixtureCPU)")
            test = cast(TestMeasure, fixture.times["tests"][0])
            self.assertEqual(setup["kind"], "fixture")
            self.assertEqual(test["kind"], "test")
            self.assertGreater(setup["cpu_s"], 0.1)
            self.assertGreater(setup["cpu_s"], test["cpu_s"])
            self.assertGreater(setup["s"], 0)
            self.assertEqual(fixture.times["tests_run"], 1)
            self.assertEqual(fixture.times["tests_s"], test["s"])
            self.assertTrue(any(row["test"] == "setUpModule (test_fixture)" for row in fixtures))

    def test_missing_arguments_print_one_usage_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, "HOME": str(root / "home"), "CONF_DIR": str(root / "config"),
                   "BUILDLOG_DIR": str(root / "buildlog")}
            _ = env.pop("TEST_TIMES_OUT", None)
            result = subprocess.run(
                [sys.executable, str(RUNNER)], cwd=root, env=env,
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr, "usage: module_run.py <start_dir> <pattern>\n")

    def test_output_and_exit_status_match_unittest(self) -> None:
        for fixture, status in ((self.outcomes, 1), (self.empty, 5), (self.success, 0)):
            with self.subTest(status=status):
                self.assertEqual(fixture.standard.returncode, status, fixture.standard.stderr)
                self.assertEqual(fixture.timed.returncode, fixture.standard.returncode, fixture.timed.stderr)
                self.assertEqual(fixture.timed.stdout, fixture.standard.stdout)
                self.assertEqual(normalized_output(fixture.timed.stderr), normalized_output(fixture.standard.stderr))
        self.assertIn("DeprecationWarning: fixture warning", self.outcomes.timed.stderr)

    def test_each_parent_has_one_outcome_with_error_precedence(self) -> None:
        rows = self.outcomes.times["tests"]
        by_name = {row["test"]: row for row in rows}
        expected = {
            "Outcomes.test_pass": "pass", "Outcomes.test_fail": "fail",
            "Outcomes.test_error": "error", "Outcomes.test_skip": "skip",
            "Outcomes.test_expected_failure": "expected_failure",
            "Outcomes.test_unexpected_success": "unexpected_success",
            "Outcomes.test_subtest_fail": "fail", "Outcomes.test_subtest_error": "error",
            "Outcomes.test_warning": "pass", "setUpClass (test_fixture.BrokenClass)": "error",
        }
        self.assertEqual({name: row["status"] for name, row in by_name.items()}, expected)
        self.assertEqual(len(rows), len(expected), "Subtests must share their parent entry")
        self.assertEqual(by_name["Outcomes.test_skip"]["s"], 0.0)
        self.assertEqual(by_name["setUpClass (test_fixture.BrokenClass)"]["s"], 0.0)
        self.assertEqual(self.outcomes.times["tests_run"], 9)
        self.assertEqual(self.outcomes.times["failures"], 3)
        self.assertEqual(self.outcomes.times["errors"], 3)
        self.assertEqual(self.outcomes.times["skipped"], 2)

    def test_totals_include_all_test_times_and_empty_discovery(self) -> None:
        for fixture in (self.outcomes, self.empty, self.success):
            with self.subTest(status=fixture.timed.returncode):
                times = fixture.times
                for value in (times["wall_s"], times["cpu_s"], times["tests_s"]):
                    self.assertIsInstance(value, (int, float))
                    self.assertTrue(math.isfinite(value))
                    self.assertGreaterEqual(value, 0.0)
                for row in times["tests"]:
                    self.assertTrue(math.isfinite(row["s"]))
                    self.assertGreaterEqual(row["s"], 0.0)
                    if row["status"] != "skip" and " (" not in row["test"]:
                        self.assertGreater(row["s"], 0.0, row["test"])
                self.assertAlmostEqual(times["tests_s"], sum(row["s"] for row in times["tests"]))
        self.assertGreater(self.outcomes.times["tests_s"], 0.0)
        self.assertGreater(self.success.times["tests_s"], 0.0)
        self.assertEqual(self.empty.times["tests"], [])
        self.assertEqual(self.empty.times["tests_run"], 0)
        self.assertEqual(self.success.times["tests_run"], 1)
        self.assertEqual([row["test"] for row in self.success.times["tests"]], ["RootImport.test_value"])

    def test_output_write_failure_keeps_the_test_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _ = (root / "test_fixture.py").write_text(textwrap.dedent(SUCCESS), encoding="utf-8")
            _ = (root / "fixture_root.py").write_text("VALUE = 42\n", encoding="utf-8")
            output = root / "output"
            output.mkdir()
            env = {**os.environ, "TEST_TIMES_OUT": str(output), "HOME": str(root / "home"),
                   "CONF_DIR": str(root / "config"), "BUILDLOG_DIR": str(root / "buildlog")}
            _ = env.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, str(RUNNER), str(root), "test_fixture.py"], cwd=root, env=env,
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            warnings = [line for line in result.stderr.splitlines() if line.startswith("test times not written:")]
            self.assertEqual(len(warnings), 1, result.stderr)
            self.assertTrue(output.is_dir())
            self.assertEqual(list(root.glob(".output.*")), [])


@unittest.skipUnless(sys.platform == "linux", "Descendant adoption uses Linux prctl and pidfds")
class ModuleProcessCleanupTests(unittest.TestCase):
    def test_ignored_sigchld_keeps_the_test_process_status_and_inheritance(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (root / "test_signal.py").write_text(
            """import signal, unittest
class InheritedSignal(unittest.TestCase):
    def test_sigchld(self):
        self.assertEqual(signal.getsignal(signal.SIGCHLD), signal.SIG_IGN)
""",
            encoding="utf-8")
        environment = {**os.environ, "HOME": str(root / "home"), "CONF_DIR": str(root / "config"),
                       "BUILDLOG_DIR": str(root / "buildlog")}
        for name in ("TEST_TIMES_OUT", "PYTHONPATH"):
            _ = environment.pop(name, None)
        launcher = ("import os, signal, sys; signal.signal(signal.SIGCHLD, signal.SIG_IGN); "
                    "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])")
        for arguments, status in (([], 2), ([str(root), "test_signal.py"], 0)):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, "-c", launcher, str(RUNNER), *arguments],
                    cwd=root, env=environment, capture_output=True, text=True, check=False,
                    timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)

    def run_orphan(self, *, fail: bool = False, terminate: bool = False, reap: bool = False) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        processes = OwnedTestProcesses()
        self.addCleanup(processes.finish)
        source = (ORPHAN_PROCESS.replace("__REAP__", repr(reap)).replace("__FAIL__", repr(fail))
                  .replace("__HANG_GUARD_MS__", str(TEST_HANG_GUARD_SECS * 1000)))
        _ = (root / "test_orphan.py").write_text(source, encoding="utf-8")
        environment = processes.environment({**os.environ, "HOME": str(root / "home"),
            "CONF_DIR": str(root / "config"), "BUILDLOG_DIR": str(root / "buildlog"),
            "PYTHONDONTWRITEBYTECODE": "1"})
        for name in ("TEST_TIMES_OUT", "PYTHONPATH"):
            _ = environment.pop(name, None)
        process: subprocess.Popen[str] = subprocess.Popen([sys.executable, str(RUNNER), str(root), "test_orphan.py"],
            cwd=root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        processes.track(process)
        output = process.stdout
        self.assertIsNotNone(output)
        if output is None:
            raise AssertionError("The runner has no output pipe")
        self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0], "The orphan did not start")
        line = cast(str, output.readline())
        self.assertTrue(line.strip().isdigit(), line)
        descriptor = os.pidfd_open(int(line))
        self.addCleanup(os.close, descriptor)
        if terminate:
            send_process_group(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(input="x", timeout=TEST_HANG_GUARD_SECS)
        expected = -signal.SIGTERM if terminate else 1 if fail else 0
        self.assertEqual(process.returncode, expected, stdout + stderr)
        # POLLHUP means the process was reaped, rather than left as a zombie.
        observer = select.poll()
        observer.register(descriptor, select.POLLHUP)
        self.assertTrue(observer.poll(0), "The orphan remains alive or unreaped")

    def test_normal_end_kills_and_reaps_a_detached_orphan(self) -> None:
        self.run_orphan()

    def test_failed_test_kills_and_reaps_a_detached_orphan(self) -> None:
        self.run_orphan(fail=True)

    def test_sigterm_to_the_runner_group_kills_and_reaps_a_detached_orphan(self) -> None:
        self.run_orphan(terminate=True)

    def test_a_running_test_sees_its_killed_orphan_reaped(self) -> None:
        self.run_orphan(reap=True)
