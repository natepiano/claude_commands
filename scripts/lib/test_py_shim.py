"""Test import resolution through the real Python shim."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import final

from scripts.lib.py_floor_test_support import cache_file_name

ROOT = Path(__file__).resolve().parents[2]
SHIM = ROOT / "scripts/lib/py"
PROBE = "from scripts.lib import unit_runs; print(unit_runs.__file__); assert callable(unit_runs.run_owner)"


@final
class PythonShimTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.environment = dict(os.environ, CLAUDE_PYTHON=sys.executable, PYTHONPATH=str(self.folder / "inherited"))
        (self.folder / "inherited").mkdir()

    def check_import(self, arguments: list[str], *, stale: bool) -> None:
        scripts = self.folder / "scripts"
        scripts.mkdir()
        _ = (scripts / "__init__.py").write_text("")
        if stale:
            library = scripts / "lib"
            library.mkdir()
            _ = (library / "__init__.py").write_text("")
            _ = (library / "unit_runs.py").write_text("# This old module has no run_owner.\n")
        result = subprocess.run(
            [str(SHIM), *arguments], input=PROBE, cwd=self.folder, env=self.environment,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), ROOT / "scripts/lib/unit_runs.py")

    def test_stdin_ignores_a_local_scripts_folder(self) -> None:
        self.check_import(["-"], stale=False)

    def test_stdin_ignores_an_old_local_library(self) -> None:
        self.check_import(["-"], stale=True)

    def test_command_ignores_a_local_scripts_folder(self) -> None:
        self.check_import(["-c", PROBE], stale=False)

    def test_command_ignores_an_old_local_library(self) -> None:
        self.check_import(["-c", PROBE], stale=True)

    def test_options_before_stdin_keep_the_install_first(self) -> None:
        self.check_import(["-u", "-W", "ignore", "-"], stale=True)

    def test_options_before_an_attached_command_keep_the_install_first(self) -> None:
        self.check_import(["-B", "-c" + PROBE], stale=True)

    def test_module_keeps_the_install_first_and_inherited_paths(self) -> None:
        _ = (self.folder / "inherited/probe.py").write_text(PROBE)
        self.check_import(["-mprobe"], stale=True)

    def test_cached_interpreter_keeps_the_install_first(self) -> None:
        cache = self.folder / "cache/claude" / cache_file_name()
        cache.parent.mkdir(parents=True)
        _ = cache.write_text(sys.executable + "\n")
        self.environment["XDG_CACHE_HOME"] = str(cache.parent.parent)
        _ = self.environment.pop("CLAUDE_PYTHON")
        self.check_import(["-"], stale=True)

    def test_relative_shim_with_parent_segments_uses_its_root(self) -> None:
        shim = ROOT / "scripts/direct/../lib/py"
        result = subprocess.run(
            ["bash", os.path.relpath(shim, self.folder), "-c", PROBE],
            cwd=self.folder, env=self.environment, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), ROOT / "scripts/lib/unit_runs.py")

    def test_file_script_keeps_its_folder_first(self) -> None:
        inherited = self.environment["PYTHONPATH"]
        self.environment["PYTHONPATH"] = os.pathsep.join((str(ROOT), inherited, str(ROOT)))
        script = self.folder / "program/probe.py"
        script.parent.mkdir()
        _ = script.write_text(
            "import subprocess, sys\nprint(sys.path[0], flush=True)\n"
            + "subprocess.run([sys.executable, '-c', \"import os; print(os.environ['PYTHONPATH'])\"], check=True)\n")
        result = subprocess.run(
            [str(SHIM), "-B", str(script)], cwd=self.folder, env=self.environment,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [str(script.parent), inherited])


if __name__ == "__main__":
    _ = unittest.main()
