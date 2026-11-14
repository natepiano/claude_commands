"""Test folder cleanup at normal exit and after a stopped test run."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from typing import cast, override
from unittest import mock

from scripts.lib import build_folders, testing_folders
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


SHIM = Path(__file__).with_name("py")
FIXTURE = """import json, os, pathlib, sys, tempfile, unittest
class Fixture(unittest.TestCase):
    def test_folder(self):
        path = pathlib.Path(tempfile.mkdtemp(prefix='leftover-'))
        (path / 'output').write_text('test output')
        print(json.dumps([os.environ['TMPDIR'], str(path), os.getpid()]), flush=True)
        sys.stdin.read(1)
"""


class TestRunFolderTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = (self.root / "test_fixture.py").write_text(FIXTURE)

    def environment(self) -> dict[str, str]:
        result = {**os.environ, "TMPDIR": str(self.root)}
        _ = result.pop("MERGE_GATE_PARENT_PID", None)
        _ = result.pop(testing_folders.RUN_FOLDER_ENV, None)
        return result

    def start(self, *options: str) -> tuple[subprocess.Popen[str], Path, Path]:
        process = subprocess.Popen([str(SHIM), *options, "-m", "unittest", "discover", "-s", str(self.root),
                                    "-p", "test_fixture.py"],
                                   cwd=self.root, env=self.environment(), text=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, process)
        assert process.stdout is not None
        with selectors.DefaultSelector() as ready:
            _ = ready.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(TEST_HANG_GUARD_SECS), "The test did not report its folder.")
        line = cast(str, process.stdout.readline())
        if not line:
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.fail("The test exited before it reported its folder: " + output + errors)
        folder, child, _ = cast(tuple[str, str, int], json.loads(line))
        return process, Path(folder), Path(child)

    def stop(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_bootstrap_removes_the_run_folder_on_normal_exit(self) -> None:
        for options in ((), ("--isolated",)):
            with self.subTest(options=options):
                process, folder, child = self.start(*options)
                self.assertEqual(folder.parent, self.root / "test-runs")
                self.assertTrue(child.is_relative_to(folder))
                self.assertEqual(build_folders.build_folder_state(folder), "held")
                output, errors = process.communicate("x", timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertFalse(folder.exists())
                self.assertEqual(list(folder.parent.iterdir()), [])

    def test_the_next_run_removes_killed_runs_and_keeps_a_live_run(self) -> None:
        live, live_folder, live_child = self.start()
        for number in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(number=number):
                stopped, folder, child = self.start()
                self.assertTrue(live_child.is_dir())
                stopped.send_signal(number)
                _ = stopped.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(stopped.returncode, -number)
                self.assertTrue(child.is_dir())
                self.assertEqual(build_folders.build_folder_state(folder), "free")
                following, following_folder, _ = self.start()
                self.assertFalse(folder.exists())
                self.assertFalse(folder.with_name(folder.name + ".lease.json").exists())
                self.assertFalse(folder.with_name(folder.name + ".lock").exists())
                self.assertEqual(build_folders.build_folder_state(live_folder), "held")
                self.assertTrue(live_child.is_dir())
                output, errors = following.communicate("x", timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(following.returncode, 0, output + errors)
                self.assertFalse(following_folder.exists())
        output, errors = live.communicate("x", timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(live.returncode, 0, output + errors)
        self.assertEqual(list(live_folder.parent.iterdir()), [])

    def test_the_context_resets_tempfile_and_restores_the_callers_environment(self) -> None:
        with mock.patch.dict(os.environ, self.environment()), mock.patch.object(tempfile, "tempdir", str(self.root)):
            with testing_folders.test_run_folder() as path:
                self.assertEqual(tempfile.gettempdir(), str(path))
                child = subprocess.run([sys.executable, "-c", "import os; print(os.environ['TMPDIR'])"],
                                       text=True, capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(child.stdout.strip(), str(path))
                with testing_folders.test_run_folder() as nested:
                    self.assertEqual(nested.parent, path.parent)
                    self.assertNotEqual(nested, path)
                    self.assertEqual(build_folders.build_folder_state(path), "held")
                self.assertEqual(os.environ["TMPDIR"], str(path))
            self.assertEqual(os.environ["TMPDIR"], str(self.root))
            self.assertEqual(tempfile.tempdir, str(self.root))
            self.assertFalse(path.exists())
