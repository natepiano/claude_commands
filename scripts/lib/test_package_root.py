"""Check entry scripts without a package environment or a checkout working directory."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PackageRootTests(unittest.TestCase):
    def test_an_entry_runs_by_relative_path_in_an_isolated_interpreter(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", "-c",
             'import runpy, sys; sys.argv = ["send.py", "--help"]; runpy.run_path("send.py", run_name="__main__")'],
            cwd=ROOT / "scripts/message",
            env={name: value for name, value in os.environ.items() if name != "PYTHONPATH"},
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertNotIn("FileNotFoundError", result.stderr)

    def test_a_copied_entry_uses_the_package_root_from_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "bin"
            folder.mkdir()
            entry = folder / "send.py"
            _ = shutil.copyfile(ROOT / "scripts/message/send.py", entry)
            result = subprocess.run(
                [sys.executable, str(entry), "--help"], cwd=directory,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("FileNotFoundError", result.stderr)

    def test_entry_help_runs_outside_the_checkout_without_pythonpath(self) -> None:
        entries = (
            "message/send.py", "production/update_registration.py",
            "production/change_review.py", "style/style_sync.py",
            "direct/board_json.py", "agents/detached_runs.py", "hooks/herdr_tab_title.py",
            "accounts/new_account.py", "whoami/codex_pacer.py", "lib/scratch.py",
        )
        environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
        with tempfile.TemporaryDirectory() as directory:
            for entry in entries:
                with self.subTest(entry=entry):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "scripts" / entry), "--help"],
                        cwd=directory, env=environment, input="", capture_output=True,
                        text=True, check=False, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_entry_selects_its_checkout_with_both_launchers_and_pythonpaths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            conflicting_checkout = folder / "conflicting"
            conflicting_package = conflicting_checkout / "scripts/prioritize"
            conflicting_package.mkdir(parents=True)
            _ = (conflicting_package / "writer_lock.py").write_text(
                'raise SystemExit("conflicting writer_lock imported")\n', encoding="utf-8",
            )
            entry = ROOT / "scripts/prioritize/runner_lock.py"
            for launcher in ("python3", str(ROOT / "scripts/lib/py")):
                for pythonpath in ("", str(conflicting_checkout)):
                    with self.subTest(launcher=launcher, pythonpath=pythonpath):
                        environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
                        if pythonpath:
                            environment["PYTHONPATH"] = pythonpath
                        result = subprocess.run(
                            [launcher, str(entry), "status", str(folder / "writer.lock")],
                            cwd=folder, env=environment, capture_output=True,
                            text=True, check=False, timeout=30,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, "free\n")
                        self.assertEqual(result.stderr, "")

    def test_an_existing_package_root_keeps_its_position_without_a_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", """import pathlib, runpy, sys
root = pathlib.Path(sys.argv[1]).resolve()
before = list(sys.path)
runpy.run_path(str(root / 'scripts/lib/package_root.py'))
assert sys.path == before, (before, sys.path)
assert sum(pathlib.Path(entry).resolve() == root for entry in sys.path) == 1
""", str(ROOT)],
                cwd=directory, env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
