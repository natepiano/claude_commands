"""Test process counts and module costs with private test modules."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import io
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.test_times import module_cost, task_cpu


class ModuleCostTests(unittest.TestCase):
    def test_groups_interpreter_commands_by_the_script_or_module(self) -> None:
        samples = [
            (["python3", "-m", "unittest", "discover", "-s", "a"], "python3 -m unittest"),
            (["python3", "-W", "ignore", "-m", "pkg.mod", "x"], "python3 -m pkg.mod"),
            (["python3", "-X", "dev", "heavy.py"], "python3 heavy.py"),
            (["python3", "-Ic", "x"], "python3 -c"),
            (["bash", "-lc", "x"], "bash -c"),
            (["bash", "-o", "pipefail", "run.sh"], "bash run.sh"),
            (["bash", "-euo", "pipefail", "-c", "x"], "bash -c"),
            (["git", "status"], "git"),
            (["pypy3", "--", "-script.py"], "pypy3 -script.py"),
            (["zsh", "-O", "option", "run.sh"], "zsh run.sh"),
            (["python3", "--verbose", "heavy.py"], "python3 heavy.py"),
            (["node", "run.js"], "node"),
        ]
        for arguments, expected in samples:
            with self.subTest(arguments=arguments):
                self.assertEqual(task_cpu.command_name(arguments), expected)

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("strace"), "Linux and strace are required")
    def test_command_measure_preserves_a_child_group_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree), os.environ.get("PYTHONPATH", ""))),
            }))
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            _ = (folder / "test_stop.py").write_text(
                "import os, signal, unittest\n"
                + "class Stop(unittest.TestCase):\n"
                + "    def test_stop(self):\n"
                + "        child = os.fork()\n"
                + "        if child == 0:\n"
                + "            os.kill(os.getpid(), signal.SIGSTOP)\n"
                + "            os._exit(0)\n"
                + "        try:\n"
                + "            pid, status = os.waitpid(child, os.WUNTRACED)\n"
                + "            self.assertEqual(pid, child)\n"
                + "            self.assertTrue(os.WIFSTOPPED(status))\n"
                + "            os.kill(child, signal.SIGCONT)\n"
                + "            self.assertEqual(os.waitpid(child, 0)[1], 0)\n"
                + "        finally:\n"
                + "            try: os.kill(child, signal.SIGKILL)\n"
                + "            except ProcessLookupError: pass\n"
                + "            try: os.waitpid(child, 0)\n"
                + "            except ChildProcessError: pass\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                status = module_cost.main(["--tree", str(tree), "--commands", "10", "fixture/test_stop.py"])
        self.assertEqual(status, 0, output.getvalue())

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_ending_the_tracer_ends_its_paused_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as sockets:
            reader, writer = socket.socketpair()
            _ = sockets.enter_context(reader)
            _ = sockets.enter_context(writer)
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree), os.environ.get("PYTHONPATH", ""))),
            }))
            paused = tree / "paused.py"
            _ = paused.write_text(
                "import os, signal\n"
                + f"os.write({writer.fileno()}, str(os.getpid()).encode())\n"
                + "signal.pause()\n", encoding="utf-8")
            tracer = tree / "tracer.py"
            _ = tracer.write_text(
                "import os, sys\nfrom pathlib import Path\n"
                + "from scripts.test_times.task_cpu import measure\n"
                + f"measure([sys.executable, {str(paused)!r}], cwd=Path({str(tree)!r}), environment=dict(os.environ))\n",
                encoding="utf-8")
            process = subprocess.Popen([str(Path(task_cpu.__file__).resolve().parents[1] / "lib/py"), str(tracer)], pass_fds=(writer.fileno(),),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            writer.close()
            child = 0
            try:
                reader.settimeout(TEST_HANG_GUARD_SECS)
                child = int(reader.recv(32))
                descriptor = os.pidfd_open(child)
                try:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, -signal.SIGTERM, stdout + stderr)
                    ended = select.poll()
                    ended.register(descriptor, select.POLLIN)
                    self.assertTrue(ended.poll(int(TEST_HANG_GUARD_SECS * 1000)), "The traced task did not end")
                finally:
                    os.close(descriptor)
            finally:
                if child:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

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
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree), os.environ.get("PYTHONPATH", ""))),
            }))
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
    def test_lists_the_test_with_a_cpu_heavy_child_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree), os.environ.get("PYTHONPATH", ""))),
            }))
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            _ = (folder / "test_cpu.py").write_text(
                "import subprocess, sys, unittest\n"
                + "class CPU(unittest.TestCase):\n"
                + "    def test_heavy(self):\n"
                + "        subprocess.run([sys.executable, '-c', "
                + "\"import time; end=time.process_time()+0.2\\nwhile time.process_time()<end: pass\"], check=True)\n"
                + "    def test_light(self):\n"
                + "        import time\n"
                + "        end=time.process_time()+0.02\n"
                + "        while time.process_time()<end: pass\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                status = module_cost.main(["--tree", str(tree), "--tests", "2", "fixture/test_cpu.py"])
        self.assertEqual(status, 0, output.getvalue())
        rows = [line.split() for line in output.getvalue().splitlines() if "CPU.test_" in line]
        self.assertEqual([row[1] for row in rows], ["CPU.test_heavy", "CPU.test_light"])
        self.assertGreater(float(rows[0][-2]), float(rows[1][-2]))
        self.assertGreater(float(rows[0][-1]), 0)

    @unittest.skipUnless(shutil.which("strace"), "strace is not on PATH")
    def test_ends_a_detached_child_and_keeps_the_module_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree), os.environ.get("PYTHONPATH", ""))),
            }))
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            pids = tree / "pids"
            _ = (folder / "test_orphan.py").write_text(
                "import pathlib, subprocess, sys, unittest\n"
                + "class Orphan(unittest.TestCase):\n"
                + "    def test_orphan(self):\n"
                + "        child=subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'], "
                + "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                + f"        with pathlib.Path({str(pids)!r}).open('a') as output: output.write(str(child.pid)+'\\n')\n"
                + "        self.assertIsNone(child.poll())\n", encoding="utf-8")
            try:
                output = io.StringIO()
                with redirect_stdout(output):
                    status = module_cost.main(["--tree", str(tree), "fixture/test_orphan.py"])
                self.assertEqual(status, 0, output.getvalue())
                children = [int(line) for line in pids.read_text().splitlines()]
                self.assertEqual(len(children), 2)
                for pid in children:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if pids.is_file():
                    for line in pids.read_text().splitlines():
                        try:
                            os.kill(int(line), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("strace"), "Linux and strace are required")
    def test_ranks_commands_by_their_cpu_and_reports_unassigned_cpu(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree), os.environ.get("PYTHONPATH", ""))),
            }))
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            heavy = tree / "heavy.py"
            light = tree / "light.py"
            _ = heavy.write_text(
                "import os, time\n"
                + "child=os.fork()\n"
                + "if child == 0:\n"
                + "    end=time.process_time()+0.4\n"
                + "    while time.process_time()<end: pass\n"
                + "    os._exit(0)\n"
                + "os.waitpid(child, 0)\n")
            _ = light.write_text("pass\n")
            _ = (folder / "test_commands.py").write_text(
                "import subprocess, sys, unittest\n"
                + "class Commands(unittest.TestCase):\n"
                + "    def test_heavy(self):\n"
                + f"        subprocess.run([sys.executable, {str(heavy)!r}], check=True)\n"
                + "    def test_light(self):\n"
                + f"        subprocess.run([sys.executable, {str(light)!r}], check=True)\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(Path(module_cost.__file__)), "--tree", str(tree),
                 "--commands", "10", "fixture/test_commands.py"],
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = result.stdout.splitlines()
        commands = rows[rows.index(next(line for line in rows if "test_commands.py command" in line)) + 1:]
        self.assertIn(str(heavy), commands[0])
        self.assertEqual(int(commands[0].split()[-2]), 2)
        light_row = next(line for line in commands if str(light) in line)
        self.assertGreater(float(commands[0].split()[-1]), float(light_row.split()[-1]))
        self.assertRegex(commands[-1], r"^unassigned [+-]\d+\.\d+ CPU s$")

    @unittest.skipUnless(shutil.which("strace"), "strace is not on PATH")
    def test_reports_costs_for_a_module_that_starts_two_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree), os.environ.get("PYTHONPATH", ""))),
            }))
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
        self.assertEqual(int(row[2]), 3)
        self.assertEqual(int(row[3]), 3)
        self.assertGreater(float(row[4]), 0)
        self.assertEqual(row[5], "PASS")
        self.assertEqual(lines[2].split(), ["total", *row[1:]])


if __name__ == "__main__":
    _ = unittest.main()
