"""Check coverage candidates with isolated repository modules."""

from __future__ import annotations

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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.git_test_support import commit_repository

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

    @classmethod
    def start_child(cls) -> None:
        cls.child = subprocess.Popen(
            [sys.executable, "-c", "from product import right; "
             "import sys; print('ready', flush=True); sys.stdin.read(); right()"],
            cwd=Path(__file__).parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.child.poll() is None:
            cls.child.kill()
        cls.child.communicate(timeout=10)
'''


class CoverageOverlapTests(unittest.TestCase):
    def run_fixture(self, sources: dict[str, str]) -> FixtureCoverage:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts" / "fixture"
            folder.mkdir(parents=True)
            tooling = root / "scripts" / "test_times"
            (tooling / "coverage_child").mkdir(parents=True)
            for source, destination in (
                (TOOL, tooling / TOOL.name),
                (TOOL.with_name("line_collector.py"), tooling / "line_collector.py"),
                (CHILD_STARTUP / "sitecustomize.py", tooling / "coverage_child" / "sitecustomize.py"),
            ):
                _ = shutil.copy2(source, destination)
            sources = {
                "scripts/fixture/product.py": PRODUCT,
                "config/test_times.conf": "COVERAGE_LEAF_PROGRAMS=git\n",
                **sources,
            }
            for relative, source in sources.items():
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = destination.write_text(textwrap.dedent(source), encoding="utf-8")
            output = root / "coverage.json"
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("GIT_", "NATEST_COVERAGE_"))}
            env.update({"HOME": str(root / "home"), "CONF_DIR": str(root / "config"),
                   "BUILDLOG_DIR": str(root / "buildlog"), "PYTHONDONTWRITEBYTECODE": "1",
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "PYTHONPATH": str(TOOL.parents[2]),
                   "COVERAGE_OUT": str(output)})
            _ = subprocess.run(
                ["git", "init", "--quiet", str(root)], env=env, capture_output=True,
                check=True, timeout=TEST_HANG_GUARD_SECS,
            )
            _ = commit_repository(root, "Create coverage fixtures", env=env)
            patterns = sorted(Path(name).name for name in sources
                              if Path(name).name.startswith("test_") and Path(name).suffix == ".py")
            result = subprocess.run(
                [sys.executable, str(tooling / TOOL.name), str(folder), *patterns], cwd=root, env=env,
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = cast("CoverageReport", json.loads(output.read_text(encoding="utf-8")))
            return FixtureCoverage(report, result.stdout)

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
        self.assertTrue(parent["candidate"])
        self.assertEqual(parent["covered_by"], [git["id"]])

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
        fixture = self.run_fixture({
            "scripts/fixture/child_launch.py": '''
from pathlib import Path
import subprocess
from product import right

try:
    subprocess.Popen([str(Path(__file__).with_name("missing-program"))])
except FileNotFoundError:
    print(right())
''',
            "scripts/fixture/test_failed_child_launch.py": PRELUDE + '''
class FailedChildLaunch(unittest.TestCase):
    def test_a_child(self) -> None:
        child = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("child_launch.py"))],
            capture_output=True, text=True, check=True)
        self.assertEqual(child.stdout.strip(), "right")

    def test_b_parent(self) -> None:
        self.assertEqual(right(), "right")
''',
        })
        child = fixture.row("test_a_child")
        parent = fixture.row("test_b_parent")
        self.assertEqual(child["status"], "pass")
        self.assertEqual(child["child"], "none")
        self.assertFalse(child["candidate"])
        self.assertTrue(parent["candidate"])
        self.assertEqual(parent["covered_by"], [child["id"]])

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
from fixture_test_support import support
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
