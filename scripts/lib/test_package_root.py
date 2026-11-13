"""Check entry scripts without a package environment or a checkout working directory."""
from __future__ import annotations

from scripts.lib.python_startup.sitecustomize import STARTUP_FILES

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
                env={**os.environ, "PYTHONPATH": str(ROOT / "scripts/lib/python_startup")},
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("FileNotFoundError", result.stderr)

    def test_entry_help_runs_outside_the_checkout_without_pythonpath(self) -> None:
        entries = (
            "message/send.py", "production/update_registration.py",
            "production/change_review.py", "style/style_sync.py",
            "production/unit_phases.py", "agents/detached_runs.py", "hooks/herdr_tab_title.py",
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
            selected_checkout = folder / "selected"
            for relative_path in (
                "scripts/prioritize/runner_lock.py", "scripts/prioritize/writer_lock.py",
                *STARTUP_FILES,
            ):
                destination = selected_checkout / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = shutil.copy2(ROOT / relative_path, destination)
            conflicting_checkout = folder / "conflicting"
            conflicting_package = conflicting_checkout / "scripts/prioritize"
            conflicting_package.mkdir(parents=True)
            _ = (conflicting_package / "writer_lock.py").write_text(
                'raise SystemExit("conflicting writer_lock imported")\n', encoding="utf-8",
            )
            entry = selected_checkout / "scripts/prioritize/runner_lock.py"
            for launcher in ("python3", str(selected_checkout / "scripts/lib/py")):
                for pythonpath in (
                    "", str(conflicting_checkout),
                    os.pathsep.join((str(conflicting_checkout), str(selected_checkout))),
                ):
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

    def test_entry_reuses_the_helper_loaded_by_the_shim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "entry.py"
            _ = entry.write_text("""import runpy, sys
compiled = []
def record(event, arguments):
    if event == 'compile' and str(arguments[1]).endswith('/python_startup/sitecustomize.py'):
        compiled.append(arguments[1])
sys.addaudithook(record)
for attempt in range(2):
    runpy.run_path(sys.argv[1])
print(compiled)
""")
            result = subprocess.run(
                [str(ROOT / "scripts/lib/py"), str(entry), str(ROOT / "scripts/lib/package_root.py")],
                cwd=directory, env=dict(os.environ), capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "[]\n")

    def test_entry_reuses_path_selection_when_search_paths_stay_the_same(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "entry.py"
            _ = entry.write_text("""import runpy, sys
helper = sys.modules['_devtools_python_startup']
def repeated_filtering(value):
    raise AssertionError('unchanged search paths were filtered again')
helper.inherited_paths = repeated_filtering
for attempt in range(2):
    runpy.run_path(sys.argv[1])
print('reused')
""")
            for options in ([], ["-I"]):
                with self.subTest(options=options):
                    result = subprocess.run(
                        [str(ROOT / "scripts/lib/py"), *options, str(entry), str(ROOT / "scripts/lib/package_root.py")],
                        cwd=directory, env=dict(os.environ), capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "reused\n")

    def test_entry_refilters_changed_paths_environment_and_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            foreign = base / "foreign"
            (foreign / "scripts").mkdir(parents=True)
            entry = base / "entry.py"
            _ = entry.write_text("""import os, runpy, sys
bootstrap, foreign = sys.argv[1:]
sys.path.insert(1, foreign)
runpy.run_path(bootstrap)
assert foreign not in sys.path, sys.path
os.environ['PYTHONPATH'] = foreign
runpy.run_path(bootstrap)
assert 'PYTHONPATH' not in os.environ, os.environ.get('PYTHONPATH')
os.environ['PYTHONPATH'] = ''
runpy.run_path(bootstrap)
assert 'PYTHONPATH' not in os.environ
sys.path.insert(1, '.')
runpy.run_path(bootstrap)
assert '.' in sys.path, sys.path
os.chdir(foreign)
runpy.run_path(bootstrap)
assert '.' not in sys.path, sys.path
print('filtered')
""")
            result = subprocess.run(
                [str(ROOT / "scripts/lib/py"), str(entry), str(ROOT / "scripts/lib/package_root.py"), str(foreign)],
                cwd=directory, env=dict(os.environ), capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "filtered\n")

    def test_entry_filters_a_foreign_package_in_its_automatic_script_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "bin"
            package = folder / "scripts"
            package.mkdir(parents=True)
            _ = (package / "__init__.py").write_text("raise AssertionError('the foreign scripts package was imported')\n")
            entry = folder / "entry.py"
            _ = entry.write_text("""import runpy, sys
from pathlib import Path
runpy.run_path(sys.argv[1])
assert str(Path(__file__).parent) not in sys.path, sys.path
import scripts
root = Path(sys.argv[1]).resolve().parents[2]
assert list(scripts.__path__) == [str(root / 'scripts')], list(scripts.__path__)
print('selected')
""")
            result = subprocess.run(
                [str(ROOT / "scripts/lib/py"), str(entry), str(ROOT / "scripts/lib/package_root.py")],
                cwd=directory, env=dict(os.environ), capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "selected\n")

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
                cwd=directory, env={**os.environ, "PYTHONPATH": str(ROOT / "scripts/lib/python_startup")},
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
