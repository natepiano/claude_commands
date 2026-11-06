"""Test process counts and module costs with private test modules."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.test_times import module_cost


class ModuleCostTests(unittest.TestCase):
    def test_counts_successful_process_and_exec_calls_without_threads(self) -> None:
        samples = [
            ('101 fork() = 102', 1, 0),
            ('101 vfork() = 103', 1, 0),
            ('101 clone(child_stack=NULL, flags=CLONE_VM|SIGCHLD) = 104', 1, 0),
            ('101 clone3({flags=CLONE_VM|CLONE_VFORK, exit_signal=SIGCHLD}, 88) = 105', 1, 0),
            ('[pid 101] execve("/bin/git", ["git", "status"], 0x1234) = 0', 0, 1),
            ('101 execve("/bin/echo", ["echo", "<unfinished ...>"], 0x1234) = 0', 0, 1),
            ('101 clone3({flags=0}, 88) = -1 ENOSYS (Function not implemented)', 0, 0),
            ('101 execve("/missing", ["missing"], 0x1234) = -1 ENOENT (No such file or directory)', 0, 0),
            ('101 clone(child_stack=0x1234, flags=CLONE_VM|CLONE_THREAD) = 106', 0, 0),
            ('101 clone3({flags=CLONE_VM|CLONE_THREAD}, 88) = 106', 0, 0),
            ('101 vfork( <unfinished ...>\n102 execve("/bin/git", ["git"], 0x1234) = 0\n'
             + '101 <... vfork resumed>) = 102', 1, 1),
            ('101 clone(child_stack=0x1234, flags=CLONE_THREAD <unfinished ...>\n'
             + '101 <... clone resumed>) = 102', 0, 0),
        ]
        for trace, processes, execs in samples:
            with self.subTest(trace=trace):
                counts = module_cost.count_starts(trace.splitlines())
                self.assertEqual(counts.processes, processes)
                self.assertEqual(counts.execs, execs)

    def test_reports_each_module_when_the_trace_start_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            modules = ["test_first.py", "test_second.py"]
            for module in modules:
                _ = (folder / module).write_text(
                    "import sys\nimport unittest\n"
                    + "class Fixture(unittest.TestCase):\n"
                    + "    def test_environment(self):\n"
                    + "        self.assertTrue(sys.dont_write_bytecode)\n",
                    encoding="utf-8",
                )
            tools = tree / "bin"
            tools.mkdir()
            strace = tools / "strace"
            _ = strace.write_text(f"#!{sys.executable}\nraise SystemExit(7)\n", encoding="utf-8")
            strace.chmod(0o755)
            result = subprocess.run(
                [sys.executable, str(Path(module_cost.__file__)), "--tree", str(tree),
                 *[f"fixture/{module}" for module in modules]],
                env=dict(os.environ, PATH=str(tools)), capture_output=True, text=True, check=False,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = result.stdout.splitlines()
        for module, row in zip(modules, rows[1:3], strict=True):
            self.assertEqual(row.split()[0], f"fixture/{module}")
            self.assertIn("RED (test=0, trace=7)", row)

    @unittest.skipUnless(shutil.which("strace"), "strace is not on PATH")
    def test_reports_costs_for_a_module_that_starts_two_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            _ = (folder / "test_children.py").write_text(
                'import os\nimport subprocess\nimport sys\nimport unittest\n'
                + 'class Children(unittest.TestCase):\n'
                + '    def test_children(self):\n'
                + '        self.assertEqual(os.environ["HERDR_BIN"], "/nonexistent/herdr")\n'
                + '        self.assertTrue(sys.dont_write_bytecode)\n'
                + '        for code in ("pass", "import os"):\n'
                + '            subprocess.run([sys.executable, "-c", code], check=True)\n',
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                status = module_cost.main(["--tree", str(tree), "fixture/test_children.py"])
        self.assertEqual(status, 0, output.getvalue())
        lines = output.getvalue().splitlines()
        row = lines[1].split()
        self.assertEqual(row[0], "fixture/test_children.py")
        self.assertGreater(float(row[1]), 0)
        self.assertEqual(int(row[2]), 2)
        self.assertEqual(int(row[3]), 3)
        self.assertGreater(float(row[4]), 0)
        self.assertEqual(row[5], "PASS")
        self.assertEqual(lines[2].split(), ["total", *row[1:]])


if __name__ == "__main__":
    _ = unittest.main()
