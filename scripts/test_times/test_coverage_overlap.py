"""Check coverage candidates with isolated repository modules."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
from typing import TYPE_CHECKING, cast
import unittest


from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.git_test_support import commit_repository
from scripts.test_times.coverage_overlap import TestCoverage, select_candidates

if TYPE_CHECKING:
    from scripts.test_times.coverage_overlap import CoverageReport, CoverageRow

TOOL = Path(__file__).with_name("coverage_overlap.py").resolve()
CHILD_STARTUP = TOOL.with_name("coverage_child")


@dataclass(frozen=True)
class FixtureCoverage:
    report: CoverageReport
    table: str

    def row(self, method: str) -> CoverageRow:
        matches = [row for row in self.report["tests"] if row["id"].endswith(f".{method}")]
        if len(matches) != 1:
            raise AssertionError(f"Expected one row for {method}: {self.report}")
        return matches[0]


PRODUCT = '''\
def left() -> str:
    return "left"

def right() -> str:
    return "right"
'''

PRELUDE = '''\
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from product import left, right

def slow() -> None:
    _ = hashlib.pbkdf2_hmac("sha256", b"fixture", b"salt", 100000)
'''

STARTUP_PRELUDE = '''\
import json
COVERAGE_CHILD = str(Path(__file__).resolve().parents[1] / "test_times" / "coverage_child")
'''

WAITING_CHILD = '''\
from typing import TextIO, cast

class WaitingChild(unittest.TestCase):
    child: subprocess.Popen[str]
    report_coverage = True

    @classmethod
    def start_child(cls) -> None:
        environment = dict(os.environ)
        if not cls.report_coverage:
            environment.pop("NATEST_COVERAGE_TEST", None)
        cls.child = subprocess.Popen(
            [sys.executable, "-c", "from product import right; "
             "import sys; print('ready', flush=True); sys.stdin.read(); right()"],
            cwd=Path(__file__).parent, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.child.poll() is None:
            cls.child.kill()
        cls.child.communicate(timeout=10)
'''

THREAD_PRELUDE = '''\
import threading

def require_event(event: threading.Event) -> None:
    if not event.wait(timeout=10):
        raise AssertionError("The worker did not reach the boundary")

def join_thread(worker: threading.Thread) -> None:
    worker.join(timeout=10)
    if worker.is_alive():
        raise AssertionError("The worker did not stop")
'''

WAITING_THREAD = THREAD_PRELUDE + '''\
class WaitingThread(unittest.TestCase):
    ready = threading.Event()
    release = threading.Event()
    values: list[str] = []
    worker: threading.Thread

    @classmethod
    def work(cls) -> None:
        cls.ready.set()
        require_event(cls.release)
        cls.values.append(right())

    @classmethod
    def start_thread(cls) -> None:
        cls.worker = threading.Thread(target=cls.work)
        cls.worker.start()
        require_event(cls.ready)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.release.set()
        join_thread(cls.worker)
'''

FAILED_EXEC_CHILD = '''\
import os
from pathlib import Path
from product import left, right

print(left(), flush=True)
missing = str(Path(__file__).with_name("missing-program"))
try:
    os.execv(missing, [missing])
except FileNotFoundError:
    print(right(), flush=True)
'''


def child_script_sources(name: str, child_source: str, parent_body: str) -> dict[str, str]:
    """Build a child script and its measured parent module."""
    parent_source = PRELUDE + f'''
def run_child() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(Path(__file__).with_name({name + ".py"!r}))],
        cwd=Path(__file__).parent, capture_output=True, text=True, check=True)
''' + parent_body
    return {
        f"scripts/fixture/{name}.py": child_source,
        f"scripts/fixture/test_{name}.py": parent_source,
    }


class CoverageOverlapTests(unittest.TestCase):
    @contextmanager
    def fixture_command(self, sources: dict[str, str]) -> Generator[tuple[subprocess.CompletedProcess[str], Path]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts" / "fixture"
            folder.mkdir(parents=True)
            tooling = root / "scripts" / "test_times"
            (tooling / "coverage_child").mkdir(parents=True)
            library = root / "scripts" / "lib"
            library.mkdir()
            for source, destination in (
                (TOOL, tooling / TOOL.name),
                (TOOL.with_name("line_collector.py"), tooling / "line_collector.py"),
                (CHILD_STARTUP / "sitecustomize.py", tooling / "coverage_child" / "sitecustomize.py"),
                (TOOL.parents[1] / "lib" / "code_kind.py", library / "code_kind.py"),
                *((TOOL.parents[1] / relative, root / "scripts" / relative) for relative in (
                    "lib/py", "lib/conf.py", "lib/terminal_cells.py", "buildlog/treekey.py",
                    "production/gate_modules.py", "test_times/module_run.py",
                )),
            ):
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = shutil.copy2(source, destination)
            _ = (root / "scripts/__init__.py").write_text("")
            sources = {
                "scripts/fixture/product.py": PRODUCT,
                "config/test_times.conf": "COVERAGE_LEAF_PROGRAMS=git\n",
                **sources,
            }
            for relative, source in sources.items():
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = destination.write_text(textwrap.dedent(source).replace(
                    "from product import", "from scripts.fixture.product import"), encoding="utf-8")
            output = root / "coverage.json"
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("GIT_", "NATEST_COVERAGE_"))}
            env.update({"HOME": str(root / "home"), "CONF_DIR": str(root / "config"),
                   "BUILDLOG_DIR": str(root / "buildlog"), "PYTHONDONTWRITEBYTECODE": "1",
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "COVERAGE_OUT": str(output)})
            _ = subprocess.run(
                ["git", "init", "--quiet", str(root)], env=env, capture_output=True,
                check=True, timeout=TEST_HANG_GUARD_SECS,
            )
            _ = commit_repository(root, "Create coverage fixtures", env=env)
            patterns = sorted(Path(name).name for name in sources
                              if Path(name).name.startswith("test_") and Path(name).suffix == ".py")
            result = subprocess.run(
                [str(library / "py"), str(tooling / TOOL.name), str(folder), *patterns], cwd=root, env=env,
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            yield result, output

    def run_fixture(self, sources: dict[str, str]) -> FixtureCoverage:
        with self.fixture_command(sources) as (result, output):
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = cast("CoverageReport", json.loads(output.read_text(encoding="utf-8")))
            return FixtureCoverage(report, result.stdout)

    def test_module_import_failure_reports_one_line_without_running_tests(self) -> None:
        with self.fixture_command({
            "scripts/fixture/test_broken.py": 'raise RuntimeError("fixture import refused")\n',
            "scripts/fixture/test_not_run.py": '''
import unittest

class MustNotRun(unittest.TestCase):
    def test_execution(self) -> None:
        print("the suite ran")
        raise AssertionError("the suite must not run")
''',
        }) as fixture:
            result = fixture[0]
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr,
                             "coverage overlap: test_broken.py does not import: RuntimeError: fixture import refused\n")

    def test_repeated_lines_remain_visible_after_the_first_test(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_repeat.py": PRELUDE + '''
class Repeat(unittest.TestCase):
    def test_a_broad(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))

    def test_z_repeat(self) -> None:
        slow()
        self.assertEqual(left(), "left")
'''})
        broad = fixture.row("test_a_broad")
        repeat = fixture.row("test_z_repeat")
        self.assertEqual((broad["line_count"], repeat["line_count"]), (2, 1))
        self.assertTrue(repeat["candidate"])
        self.assertEqual(repeat["covered_by"], [broad["id"]])
        self.assertFalse(broad["candidate"])
        self.assertEqual(repeat["status"], "pass")
        self.assertGreater(repeat["seconds"], broad["seconds"])
        self.assertIn(repeat["id"], fixture.table)
        self.assertIn(broad["id"], fixture.table)

    def test_one_unique_line_prevents_a_candidate(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_unique.py": PRELUDE + '''
class Unique(unittest.TestCase):
    def test_left(self) -> None:
        self.assertEqual(left(), "left")

    def test_right(self) -> None:
        self.assertEqual(right(), "right")
'''})
        for method in ("test_left", "test_right"):
            with self.subTest(method=method):
                row = fixture.row(method)
                self.assertEqual(row["line_count"], 1)
                self.assertFalse(row["candidate"])

    def test_equal_tests_remove_only_the_slower_test(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_equal.py": PRELUDE + '''
class Equal(unittest.TestCase):
    def test_fast(self) -> None:
        self.assertEqual(left(), "left")

    def test_slow(self) -> None:
        slow()
        self.assertEqual(left(), "left")
'''})
        fast = fixture.row("test_fast")
        slow = fixture.row("test_slow")
        self.assertGreater(slow["seconds"], fast["seconds"])
        self.assertTrue(slow["candidate"])
        self.assertEqual(slow["covered_by"], [fast["id"]])
        self.assertFalse(fast["candidate"])
        self.assertEqual((fast["line_count"], slow["line_count"]), (1, 1))

    def test_kept_tests_can_cover_a_candidate_together(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_union.py": PRELUDE + '''
class Union(unittest.TestCase):
    def test_both(self) -> None:
        slow()
        self.assertEqual((left(), right()), ("left", "right"))

    def test_left(self) -> None:
        self.assertEqual(left(), "left")

    def test_right(self) -> None:
        self.assertEqual(right(), "right")
'''})
        both = fixture.row("test_both")
        left = fixture.row("test_left")
        right = fixture.row("test_right")
        self.assertTrue(both["candidate"])
        self.assertEqual(set(both["covered_by"]), {left["id"], right["id"]})
        self.assertFalse(left["candidate"])
        self.assertFalse(right["candidate"])

    def test_python_child_lines_can_cover_another_test(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_child.py": PRELUDE + '''
class Child(unittest.TestCase):
    def test_a_child(self) -> None:
        subprocess.run([sys.executable, "-c", "from product import right; right()"],
                       cwd=Path(__file__).parent, check=True)

    def test_z_parent(self) -> None:
        self.assertEqual(right(), "right")
'''})
        child = fixture.row("test_a_child")
        parent = fixture.row("test_z_parent")
        self.assertEqual(child["child"], "none")
        self.assertFalse(child["candidate"])
        self.assertEqual((child["line_count"], parent["line_count"]), (3, 1))
        self.assertTrue(parent["candidate"])
        self.assertEqual(parent["covered_by"], [child["id"]])

    def test_isolated_python_child_cannot_cover_another_test(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_isolated.py": PRELUDE + '''
class Isolated(unittest.TestCase):
    def test_a_child(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
        subprocess.run([sys.executable, "-I", "-c", "pass"], check=True)

    def test_z_parent(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
'''})
        child = fixture.row("test_a_child")
        parent = fixture.row("test_z_parent")
        self.assertEqual(child["child"], "unknown: 1 python children, 0 reported")
        self.assertFalse(child["candidate"])
        self.assertFalse(parent["candidate"])
        self.assertEqual(parent["line_count"], 2)
        self.assertEqual(parent["child"], "none")

    def test_git_child_keeps_coverage_known(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_git.py": PRELUDE + '''
class Git(unittest.TestCase):
    def test_a_git(self) -> None:
        self.assertEqual(left(), "left")
        subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL, check=True)

    def test_z_parent(self) -> None:
        slow()
        self.assertEqual(left(), "left")
'''})
        git = fixture.row("test_a_git")
        parent = fixture.row("test_z_parent")
        self.assertEqual(git["child"], "none")
        self.assertEqual(parent["child"], "none")
        self.assertEqual((git["line_count"], parent["line_count"]), (1, 1))
        slower, faster = sorted((git, parent), key=lambda row: (-row["seconds"], row["id"]))
        self.assertTrue(slower["candidate"])
        self.assertEqual(slower["covered_by"], [faster["id"]])
        self.assertFalse(faster["candidate"])

    def test_known_git_and_parent_coverage_selects_the_slower_test_in_both_orders(self) -> None:
        lines = frozenset({("scripts/fixture/product.py", 2)})
        for git_seconds, parent_seconds, candidate, covering in (
            (2.0, 1.0, "git", "parent"),
            (1.0, 2.0, "parent", "git"),
        ):
            with self.subTest(git_seconds=git_seconds, parent_seconds=parent_seconds):
                tests = [TestCoverage("git", "pass", git_seconds, lines, "none"),
                         TestCoverage("parent", "pass", parent_seconds, lines, "none")]
                self.assertEqual(select_candidates(tests), {candidate: [covering]})

    def test_mocked_popen_preserves_child_coverage(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_mocked_popen.py": PRELUDE + '''
from unittest import mock

class MockedPopen(unittest.TestCase):
    def test_child(self) -> None:
        launch = subprocess.Popen
        with mock.patch.object(subprocess, "Popen", side_effect=launch):
            git = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
            self.assertTrue(git.stdout.startswith("git version "))
            child = subprocess.run(
                [sys.executable, "-c", "from product import right; print(right())"],
                cwd=Path(__file__).parent, capture_output=True, text=True, check=True)
            self.assertEqual(child.stdout.strip(), "right")
        self.assertEqual(left(), "left")
'''})
        child = fixture.row("test_child")
        self.assertEqual(child["status"], "pass")
        self.assertEqual(child["child"], "none")
        self.assertEqual(child["line_count"], 4)

    def test_shell_launch_cannot_cover_another_test(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_shell.py": PRELUDE + '''
import shlex

class Shell(unittest.TestCase):
    def test_a_shell(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
        command = shlex.join([sys.executable, "-I", "-c", "pass"])
        subprocess.run(["sh", "-c", command], check=True)

    def test_z_parent(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
'''})
        shell = fixture.row("test_a_shell")
        parent = fixture.row("test_z_parent")
        self.assertEqual(shell["child"], "unknown: unverified launch sh")
        self.assertFalse(shell["candidate"])
        self.assertEqual(parent["child"], "none")
        self.assertFalse(parent["candidate"])

    def test_child_outliving_its_test_marks_overlapping_tests_unknown(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_overlap.py": PRELUDE + WAITING_CHILD + '''
class Overlap(WaitingChild):
    def test_a_start(self) -> None:
        type(self).start_child()
        self.assertEqual(cast(TextIO, self.child.stdout).readline().strip(), "ready")
        self.assertEqual(left(), "left")

    def test_b_overlap(self) -> None:
        self.assertEqual(right(), "right")
        self.child.communicate(input="finish", timeout=10)
        self.assertEqual(self.child.returncode, 0)

    def test_c_after_exit(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
'''})
        for method in ("test_a_start", "test_b_overlap"):
            with self.subTest(method=method):
                row = fixture.row(method)
                self.assertEqual(row["child"], "unknown: child outlived its test")
                self.assertFalse(row["candidate"])
        after_exit = fixture.row("test_c_after_exit")
        self.assertEqual(after_exit["child"], "none")
        self.assertFalse(after_exit["candidate"])

    def test_class_setup_child_marks_only_its_class_unknown(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_shared.py": PRELUDE + WAITING_CHILD + '''
class Shared(WaitingChild):
    @classmethod
    def setUpClass(cls) -> None:
        cls.start_child()
        if cast(TextIO, cls.child.stdout).readline().strip() != "ready":
            raise AssertionError("The shared child did not start")

    def test_a_left(self) -> None:
        self.assertEqual(left(), "left")

    def test_b_right(self) -> None:
        self.assertEqual(right(), "right")
        self.child.communicate(input="finish", timeout=10)
        self.assertEqual(self.child.returncode, 0)

class Unshared(unittest.TestCase):
    def test_z_unshared(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
'''})
        for method in ("test_a_left", "test_b_right"):
            with self.subTest(method=method):
                row = fixture.row(method)
                self.assertEqual(row["child"], "unknown: shared child")
                self.assertFalse(row["candidate"])
        unshared = fixture.row("test_z_unshared")
        self.assertEqual(unshared["child"], "none")
        self.assertFalse(unshared["candidate"])

    def test_completed_class_setup_children_keep_tests_known(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_completed_setup.py": PRELUDE + '''
class Completed(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL, check=True)
        subprocess.run([sys.executable, "-c", "from product import right; right()"],
                       cwd=Path(__file__).parent, check=True)

    def test_left(self) -> None:
        self.assertEqual(left(), "left")

    def test_right(self) -> None:
        self.assertEqual(right(), "right")
'''})
        for method in ("test_left", "test_right"):
            with self.subTest(method=method):
                row = fixture.row(method)
                self.assertEqual(row["child"], "none")
                self.assertEqual(row["line_count"], 1)
                self.assertFalse(row["candidate"])

    def test_setup_python_without_report_marks_only_live_tests_unknown(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_setup_missing.py": PRELUDE + WAITING_CHILD + '''
class SetupMissing(WaitingChild):
    report_coverage = False

    @classmethod
    def setUpClass(cls) -> None:
        cls.start_child()
        if cast(TextIO, cls.child.stdout).readline().strip() != "ready":
            raise AssertionError("The shared child did not start")

    def test_a_live(self) -> None:
        self.assertEqual(left(), "left")

    def test_b_finish(self) -> None:
        self.child.communicate(input="finish", timeout=10)
        self.assertEqual(self.child.returncode, 0)
        self.assertEqual(right(), "right")

    def test_c_after(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
'''})
        for method in ("test_a_live", "test_b_finish"):
            with self.subTest(method=method):
                row = fixture.row(method)
                self.assertEqual(row["status"], "pass")
                self.assertEqual(row["child"], "unknown: shared child")
                self.assertFalse(row["candidate"])
        after = fixture.row("test_c_after")
        self.assertEqual(after["child"], "none")
        self.assertEqual(after["line_count"], 2)
        self.assertFalse(after["candidate"])

    def test_owned_child_exit_between_tests_keeps_the_next_test_known(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_between.py": PRELUDE + WAITING_CHILD + '''
class AOwner(WaitingChild):
    def test_a_start(self) -> None:
        type(self).start_child()
        self.assertEqual(cast(TextIO, self.child.stdout).readline().strip(), "ready")
        self.assertEqual(left(), "left")

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.child.communicate(input="finish", timeout=10)
            if cls.child.returncode != 0:
                raise AssertionError("The child failed between tests")
        finally:
            super().tearDownClass()

class BAfter(unittest.TestCase):
    def test_b_after(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
'''})
        owner = fixture.row("test_a_start")
        after = fixture.row("test_b_after")
        self.assertEqual(owner["child"], "unknown: child outlived its test")
        self.assertFalse(owner["candidate"])
        self.assertEqual(after["child"], "none")
        self.assertEqual(after["line_count"], 2)
        self.assertFalse(after["candidate"])

    def test_thread_from_an_earlier_test_cannot_give_lines_to_a_later_test(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_thread_owner.py": PRELUDE + WAITING_THREAD + '''
class ThreadOwner(WaitingThread):
    def test_a_start(self) -> None:
        type(self).start_thread()
        self.assertEqual(left(), "left")

    def test_b_shared(self) -> None:
        self.assertEqual(left(), "left")
        self.release.set()
        join_thread(self.worker)
        self.assertEqual(self.values, ["right"])

    def test_c_after(self) -> None:
        self.assertEqual(right(), "right")
'''})
        owner = fixture.row("test_a_start")
        shared = fixture.row("test_b_shared")
        after = fixture.row("test_c_after")
        self.assertEqual(owner["child"], "unknown: thread outlived its test")
        self.assertEqual(shared["child"], "unknown: shared thread")
        self.assertEqual((owner["line_count"], shared["line_count"], after["line_count"]), (1, 1, 1))
        for row in (owner, shared, after):
            self.assertFalse(row["candidate"])
        self.assertEqual(after["child"], "none")

    def test_shared_thread_is_seen_after_the_runner_runs_the_same_line(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_thread_line.py": PRELUDE + WAITING_THREAD + '''
class SameLine(WaitingThread):
    @classmethod
    def setUpClass(cls) -> None:
        cls.start_thread()

    def test_a_same_line(self) -> None:
        self.assertEqual(right(), "right")
        self.release.set()
        join_thread(self.worker)
        self.assertEqual(self.values, ["right"])

    def test_b_after(self) -> None:
        self.assertEqual(left(), "left")
'''})
        shared = fixture.row("test_a_same_line")
        self.assertEqual(shared["child"], "unknown: shared thread")
        self.assertEqual(shared["line_count"], 1)
        self.assertFalse(shared["candidate"])
        self.assertEqual(fixture.row("test_b_after")["child"], "none")

    def test_child_launched_by_a_setup_thread_has_no_test_owner(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_thread_child.py": PRELUDE + THREAD_PRELUDE + '''
class ThreadChild(unittest.TestCase):
    start = threading.Event()
    running = threading.Event()
    finish = threading.Event()
    values: list[str] = []
    worker: threading.Thread

    @classmethod
    def work(cls) -> None:
        require_event(cls.start)
        with subprocess.Popen(
                [sys.executable, "-c", "from product import right; "
                 "import sys; print('ready', flush=True); sys.stdin.read(); print(right())"],
                cwd=Path(__file__).parent, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, text=True) as child:
            if child.stdout is None or child.stdout.readline().strip() != "ready":
                raise AssertionError("The worker child did not start")
            cls.running.set()
            require_event(cls.finish)
            output = child.communicate(input="finish", timeout=10)[0]
            if child.returncode != 0:
                raise AssertionError("The worker child failed")
            cls.values.append(output.strip())

    @classmethod
    def setUpClass(cls) -> None:
        cls.worker = threading.Thread(target=cls.work)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.start.set()
        cls.finish.set()
        join_thread(cls.worker)

    def test_a_thread_child(self) -> None:
        self.start.set()
        require_event(self.running)
        self.assertEqual(left(), "left")
        self.finish.set()
        join_thread(self.worker)
        self.assertEqual(self.values, ["right"])

    def test_b_parent(self) -> None:
        self.assertEqual(right(), "right")
'''})
        shared = fixture.row("test_a_thread_child")
        parent = fixture.row("test_b_parent")
        self.assertEqual(shared["child"], "unknown: shared thread")
        self.assertEqual(shared["line_count"], 1)
        self.assertFalse(shared["candidate"])
        self.assertEqual(parent["child"], "none")
        self.assertEqual(parent["line_count"], 1)
        self.assertFalse(parent["candidate"])

    @unittest.skipUnless(hasattr(os, "posix_spawn"), "The fixture needs os.posix_spawn")
    def test_launch_without_popen_or_report_stays_unknown_for_later_tests(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_spawn.py": PRELUDE + '''
class Spawn(unittest.TestCase):
    def test_a_spawn(self) -> None:
        child = os.posix_spawn(sys.executable, [sys.executable, "-I", "-c", "pass"], dict(os.environ))
        self.assertEqual(os.waitpid(child, 0), (child, 0))
        self.assertEqual(left(), "left")

    def test_b_later(self) -> None:
        self.assertEqual(right(), "right")
'''})
        first = fixture.row("test_a_spawn")
        self.assertIn("1 python children, 0 reported", first["child"])
        self.assertIn("child outlived its test", first["child"])
        self.assertEqual(fixture.row("test_b_later")["child"], "unknown: child outlived its test")
        for method in ("test_a_spawn", "test_b_later"):
            row = fixture.row(method)
            self.assertEqual(row["status"], "pass")
            self.assertEqual(row["line_count"], 1)
            self.assertFalse(row["candidate"])

    def test_python_child_without_report_cannot_cover_another_test(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_missing.py": PRELUDE + '''
class Missing(unittest.TestCase):
    def test_a_missing(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
        env = dict(os.environ)
        env.pop("NATEST_COVERAGE_TEST", None)
        subprocess.run([sys.executable, "-c", "pass"], env=env, check=True)

    def test_z_parent(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
'''})
        missing = fixture.row("test_a_missing")
        parent = fixture.row("test_z_parent")
        self.assertEqual(missing["child"], "unknown: 1 python children, 0 reported")
        self.assertFalse(missing["candidate"])
        self.assertEqual(parent["child"], "none")
        self.assertFalse(parent["candidate"])

    def test_failed_launch_keeps_its_test_and_the_next_test_known(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_failed_launch.py": PRELUDE + '''
class FailedLaunch(unittest.TestCase):
    def test_a_failed_launch(self) -> None:
        with self.assertRaises(FileNotFoundError):
            subprocess.Popen([str(Path(__file__).with_name("missing-program"))])
        with self.assertRaises(FileNotFoundError):
            subprocess.Popen([str(Path(__file__).with_name("missing-program"))],
                             cwd=Path(__file__).parent)
        self.assertEqual(left(), "left")

    def test_b_next(self) -> None:
        self.assertEqual(right(), "right")
'''})
        for method in ("test_a_failed_launch", "test_b_next"):
            with self.subTest(method=method):
                row = fixture.row(method)
                self.assertEqual(row["status"], "pass")
                self.assertEqual(row["child"], "none")
                self.assertEqual(row["line_count"], 1)
                self.assertFalse(row["candidate"])

    def test_python_child_catching_failed_launch_still_reports_lines(self) -> None:
        fixture = self.run_fixture(child_script_sources("child_launch", '''
from pathlib import Path
import subprocess
from product import right

try:
    subprocess.Popen([str(Path(__file__).with_name("missing-program"))])
except FileNotFoundError:
    print(right())
''', '''
class FailedChildLaunch(unittest.TestCase):
    def test_a_child(self) -> None:
        child = run_child()
        self.assertEqual(child.stdout.strip(), "right")

    def test_b_parent(self) -> None:
        self.assertEqual(right(), "right")
'''))
        child = fixture.row("test_a_child")
        parent = fixture.row("test_b_parent")
        self.assertEqual(child["status"], "pass")
        self.assertEqual(child["child"], "none")
        self.assertFalse(child["candidate"])
        self.assertTrue(parent["candidate"])
        self.assertEqual(parent["covered_by"], [child["id"]])

    def test_caught_failed_exec_keeps_the_runner_collecting(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_failed_exec.py": PRELUDE + '''
class FailedExec(unittest.TestCase):
    def test_a_failed_exec(self) -> None:
        missing = str(Path(__file__).with_name("missing-program"))
        with self.assertRaises(FileNotFoundError):
            os.execv(missing, [missing])
        self.assertEqual((left(), right()), ("left", "right"))

    def test_b_after(self) -> None:
        self.assertEqual(right(), "right")
'''})
        failed = fixture.row("test_a_failed_exec")
        after = fixture.row("test_b_after")
        self.assertEqual(failed["status"], "pass")
        self.assertEqual(failed["child"], "none")
        self.assertEqual(failed["line_count"], 2)
        self.assertFalse(failed["candidate"])
        self.assertEqual(after["status"], "pass")
        self.assertEqual(after["child"], "none")
        self.assertTrue(after["candidate"])
        self.assertEqual(after["covered_by"], [failed["id"]])

    def test_failed_exec_child_final_report_keeps_lines_after_the_snapshot(self) -> None:
        fixture = self.run_fixture(child_script_sources("failed_exec_child", FAILED_EXEC_CHILD, '''
class FailedExecChild(unittest.TestCase):
    def test_a_child(self) -> None:
        child = run_child()
        self.assertEqual(child.stdout.splitlines(), ["left", "right"])

    def test_b_parent(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
'''))
        child = fixture.row("test_a_child")
        parent = fixture.row("test_b_parent")
        self.assertEqual(child["status"], "pass")
        self.assertEqual(child["child"], "none")
        self.assertFalse(child["candidate"])
        self.assertEqual(parent["line_count"], 2)
        self.assertTrue(parent["candidate"])
        self.assertEqual(parent["covered_by"], [child["id"]])

    def test_snapshot_without_a_final_report_cannot_cover_another_test(self) -> None:
        fixture = self.run_fixture(child_script_sources("snapshot_child", FAILED_EXEC_CHILD + "os._exit(0)\n", '''
class SnapshotChild(unittest.TestCase):
    def test_a_child(self) -> None:
        child = run_child()
        self.assertEqual(child.stdout.splitlines(), ["left", "right"])

    def test_b_parent(self) -> None:
        self.assertEqual(left(), "left")
'''))
        child = fixture.row("test_a_child")
        parent = fixture.row("test_b_parent")
        self.assertEqual(child["status"], "pass")
        self.assertEqual(child["child"], "unknown: 1 python children, 0 reported")
        self.assertFalse(child["candidate"])
        self.assertEqual(parent["child"], "none")
        self.assertFalse(parent["candidate"])

    def test_successful_exec_keeps_lines_from_both_collector_instances(self) -> None:
        fixture = self.run_fixture(child_script_sources("transition_child", '''
import os
import sys
from product import left

print(left(), flush=True)
os.execv(sys.executable, [sys.executable, "-c", "from product import right; print(right())"])
''', '''
class TransitionChild(unittest.TestCase):
    def test_a_child(self) -> None:
        child = run_child()
        self.assertEqual(child.stdout.splitlines(), ["left", "right"])

    def test_b_parent(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
'''))
        child = fixture.row("test_a_child")
        parent = fixture.row("test_b_parent")
        self.assertEqual(child["child"], "none")
        self.assertFalse(child["candidate"])
        self.assertTrue(parent["candidate"])
        self.assertEqual(parent["covered_by"], [child["id"]])

    @unittest.skipUnless(hasattr(os, "fork"), "The fixture needs os.fork")
    def test_python_exec_from_a_worker_fork_keeps_shared_ownership(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_worker_exec.py": PRELUDE + THREAD_PRELUDE + '''
class WorkerExec(unittest.TestCase):
    release = threading.Event()
    statuses: list[int] = []
    worker: threading.Thread

    @classmethod
    def work(cls) -> None:
        require_event(cls.release)
        child = os.fork()
        if child == 0:
            os.chdir(Path(__file__).parent)
            os.execv(sys.executable, [sys.executable, "-c", "from product import right; right()"])
            os._exit(1)
        cls.statuses.append(os.waitpid(child, 0)[1])

    @classmethod
    def setUpClass(cls) -> None:
        cls.worker = threading.Thread(target=cls.work)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.release.set()
        join_thread(cls.worker)

    def test_a_worker_exec(self) -> None:
        self.assertEqual(left(), "left")
        self.release.set()
        join_thread(self.worker)
        self.assertEqual(self.statuses, [0])

    def test_b_parent(self) -> None:
        self.assertEqual((left(), right()), ("left", "right"))
'''})
        shared = fixture.row("test_a_worker_exec")
        parent = fixture.row("test_b_parent")
        self.assertEqual(shared["status"], "pass")
        self.assertIn("shared thread", shared["child"])
        self.assertEqual(shared["line_count"], 1)
        self.assertFalse(shared["candidate"])
        self.assertEqual(parent["child"], "none")
        self.assertFalse(parent["candidate"])

    def test_consecutive_failed_execs_leave_no_launch_or_cause(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_repeated_exec.py": PRELUDE + '''
class RepeatedExec(unittest.TestCase):
    def test_a_failures(self) -> None:
        missing = str(Path(__file__).with_name("missing-program"))
        with self.assertRaises(FileNotFoundError):
            os.execv(missing, [missing])
        with self.assertRaises(FileNotFoundError):
            os.execv(missing, [missing])
        self.assertEqual((left(), right()), ("left", "right"))

    def test_b_after(self) -> None:
        self.assertEqual(right(), "right")
'''})
        failed = fixture.row("test_a_failures")
        after = fixture.row("test_b_after")
        self.assertEqual(failed["status"], "pass")
        self.assertEqual(failed["child"], "none")
        self.assertEqual(failed["line_count"], 2)
        self.assertFalse(failed["candidate"])
        self.assertEqual(after["child"], "none")
        self.assertTrue(after["candidate"])
        self.assertEqual(after["covered_by"], [failed["id"]])

    def test_runner_failed_exec_writes_no_child_report_or_cause(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_runner_report.py": PRELUDE + '''
class RunnerReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run(["sh", "-c", "true"], check=True)

    def test_a_failed_exec(self) -> None:
        directory = Path(os.environ["NATEST_COVERAGE_DIR"])
        before = sorted(directory.glob("*.json"))
        missing = str(Path(__file__).with_name("missing-program"))
        with self.assertRaises(FileNotFoundError):
            os.execv(missing, [missing])
        self.assertEqual(left(), "left")
        self.assertEqual(sorted(directory.glob("*.json")), before)

    def test_b_after(self) -> None:
        self.assertEqual(right(), "right")
'''})
        for method in ("test_a_failed_exec", "test_b_after"):
            with self.subTest(method=method):
                row = fixture.row(method)
                self.assertEqual(row["status"], "pass")
                self.assertEqual(row["child"], "none")
                self.assertEqual(row["line_count"], 1)
                self.assertFalse(row["candidate"])

    def test_stopped_collector_does_not_change_launch_environment(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_stopped.py": PRELUDE + '''
import atexit
import json

def launch_after_collection() -> None:
    launches: list[list[object]] = []
    for explicit in (True, False):
        os.environ.pop("NATEST_COVERAGE_LAUNCH", None)
        environment = dict(os.environ) if explicit else None
        with subprocess.Popen(
                [sys.executable, "-c", "import json, os; "
                 "print(json.dumps(os.environ.get('NATEST_COVERAGE_LAUNCH')))"],
                env=environment, stdout=subprocess.PIPE, text=True) as child:
            output = child.communicate(timeout=10)[0]
            if child.returncode != 0:
                raise AssertionError("The child failed after collection")
        changed = "NATEST_COVERAGE_LAUNCH" in (environment if environment is not None else os.environ)
        launches.append([json.loads(output), changed])
    print("Stopped launches: " + json.dumps(launches))

atexit.register(launch_after_collection)

class Stopped(unittest.TestCase):
    def test_run(self) -> None:
        self.assertEqual(left(), "left")
'''})
        self.assertEqual(fixture.row("test_run")["status"], "pass")
        self.assertIn("Stopped launches: [[null, false], [null, false]]", fixture.table)

    def test_child_chains_the_next_sitecustomize(self) -> None:
        _ = self.run_fixture({
            "next/sitecustomize.py": '''
from pathlib import Path
marker = Path(__file__).with_name("marker")
_ = marker.write_text("The next startup runs", encoding="utf-8")
''',
            "scripts/fixture/test_chain.py": PRELUDE + STARTUP_PRELUDE + '''
class Chain(unittest.TestCase):
    def test_chain(self) -> None:
        following = Path(__file__).resolve().parents[2] / "next"
        for active in (True, False):
            with self.subTest(active=active):
                marker = following / "marker"
                marker.unlink(missing_ok=True)
                env = dict(os.environ)
                env["PYTHONPATH"] = os.pathsep.join(
                    [COVERAGE_CHILD, str(following), env.get("PYTHONPATH", "")])
                if not active:
                    env.pop("NATEST_COVERAGE_TEST", None)
                subprocess.run([sys.executable, "-c", "pass"], env=env, check=True)
                self.assertEqual(marker.read_text(encoding="utf-8"), "The next startup runs")
''',
        })

    @unittest.skipUnless("sitecustomize" in sys.modules, "The interpreter has no sitecustomize of its own")
    def test_child_preserves_nix_pythonpath(self) -> None:
        _ = self.run_fixture({
            "nix_extra/fixture_nix_path.py": 'VALUE = "The Nix path remains"\n',
            "scripts/fixture/test_nix.py": PRELUDE + STARTUP_PRELUDE + '''
class Nix(unittest.TestCase):
    def test_nix(self) -> None:
        extra = Path(__file__).resolve().parents[2] / "nix_extra"
        env = dict(os.environ)
        env["NIX_PYTHONPATH"] = os.pathsep.join([str(extra), env.get("NIX_PYTHONPATH", "")])
        child = subprocess.run(
            [sys.executable, "-c", "import json, sys, fixture_nix_path; "
             "print(json.dumps([fixture_nix_path.VALUE, sys.path]))"],
            env=env, text=True, capture_output=True, check=True)
        value, paths = json.loads(child.stdout)
        self.assertEqual(value, "The Nix path remains")
        self.assertIn(str(extra), paths)
''',
        })

    def test_inactive_child_startup_writes_no_coverage(self) -> None:
        _ = self.run_fixture({"scripts/fixture/test_inactive.py": PRELUDE + STARTUP_PRELUDE + '''
class Inactive(unittest.TestCase):
    def test_inactive(self) -> None:
        output = Path(__file__).resolve().parents[2] / "inactive_output"
        output.mkdir()
        env = dict(os.environ)
        env.pop("NATEST_COVERAGE_TEST", None)
        env["NATEST_COVERAGE_DIR"] = str(output)
        env["PYTHONPATH"] = os.pathsep.join([COVERAGE_CHILD, env.get("PYTHONPATH", "")])
        child = subprocess.run(
            [sys.executable, "-c", "from product import right; print(right())"],
            cwd=Path(__file__).parent, env=env, text=True, capture_output=True, check=True)
        self.assertEqual(child.stdout.strip(), "right")
        self.assertEqual(list(output.iterdir()), [])
'''})

    def test_test_support_and_outside_lines_do_not_count(self) -> None:
        fixture = self.run_fixture({
            "outside.py": 'def outside() -> str:\n    return "outside"\n',
            "scripts/fixture/fixture_test_support.py": 'def support() -> str:\n    return "support"\n',
            "scripts/fixture/test_filter.py": PRELUDE + '''
from scripts.fixture.fixture_test_support import support
from outside import outside

def helper_in_test() -> str:
    return "test"

class Filter(unittest.TestCase):
    def test_a_plain(self) -> None:
        self.assertEqual(left(), "left")

    def test_z_filtered(self) -> None:
        slow()
        self.assertEqual(left(), "left")
        self.assertEqual((support(), outside(), helper_in_test()), ("support", "outside", "test"))
''',
        })
        plain = fixture.row("test_a_plain")
        filtered = fixture.row("test_z_filtered")
        self.assertTrue(filtered["candidate"])
        self.assertEqual(filtered["line_count"], 1)
        self.assertEqual(filtered["covered_by"], [plain["id"]])

    def test_named_modules_share_one_coverage_run(self) -> None:
        fixture = self.run_fixture({
            "scripts/fixture/test_first.py": PRELUDE + '''
class First(unittest.TestCase):
    def test_first(self) -> None:
        self.assertEqual(left(), "left")
''',
            "scripts/fixture/test_second.py": PRELUDE + '''
class Second(unittest.TestCase):
    def test_second(self) -> None:
        slow()
        self.assertEqual(left(), "left")
''',
        })
        first = fixture.row("test_first")
        second = fixture.row("test_second")
        self.assertTrue(second["candidate"])
        self.assertEqual(second["covered_by"], [first["id"]])
        self.assertFalse(first["candidate"])

    @unittest.skipUnless(hasattr(os, "fork"), "The fixture needs os.fork")
    def test_fork_marks_coverage_unknown(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_fork.py": PRELUDE + '''
class Fork(unittest.TestCase):
    def test_fork(self) -> None:
        self.assertEqual(left(), "left")
        child = os.fork()
        if child == 0:
            os._exit(0)
        self.assertEqual(os.waitpid(child, 0), (child, 0))
'''})
        row = fixture.row("test_fork")
        self.assertEqual(row["child"], "unknown: fork")
        self.assertFalse(row["candidate"])

    @unittest.skipUnless(hasattr(os, "fork"), "The fixture needs os.fork")
    def test_fork_with_python_exec_keeps_child_coverage_known(self) -> None:
        fixture = self.run_fixture({"scripts/fixture/test_exec.py": PRELUDE + '''
class Exec(unittest.TestCase):
    def test_a_exec(self) -> None:
        os.chdir(Path(__file__).parent)
        child = os.fork()
        if child == 0:
            os.execl(sys.executable, sys.executable, "-c", "from product import right; right()")
        self.assertEqual(os.waitpid(child, 0), (child, 0))

    def test_z_parent(self) -> None:
        self.assertEqual(right(), "right")
'''})
        child = fixture.row("test_a_exec")
        parent = fixture.row("test_z_parent")
        self.assertEqual(child["child"], "none")
        self.assertEqual(child["line_count"], 3)
        self.assertTrue(parent["candidate"])
        self.assertEqual(parent["covered_by"], [child["id"]])


if __name__ == "__main__":
    _ = unittest.main()
