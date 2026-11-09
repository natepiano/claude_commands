"""The entry scripts import repository modules from an unrelated folder."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import cast

from scripts.buildlog.test_index import use_test_log
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

use_test_log()


class RepoRootImportTests(unittest.TestCase):
    def test_entry_scripts_import_without_pythonpath(self) -> None:
        buildlog = Path(__file__).resolve().parent
        # Health has no no-op command, so load its path without its main block.
        health_prefix = ("-P", "-c", "import runpy, sys; runpy.run_path(sys.argv[1])")
        cases: tuple[tuple[str, tuple[str, ...], list[str], int], ...] = (
            ("cli.py", (), ["help"], 0),
            ("record.py", (), [], 2),
            ("mac_idle.py", (), [], 2),
            ("step_context.py", (), [], 2),
            ("health.py", health_prefix, [], 0),
            ("port_lint.py", (), [], 2),
            ("width_trial.py", (), ["--help"], 0),
        )
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            package = folder / "scripts"
            package.mkdir()
            _ = (package / "__init__.py").write_text("raise ImportError('unrelated scripts package')\n")
            environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            processes = [(name, status, subprocess.Popen(
                [sys.executable, *prefix, str(buildlog / name), *arguments],
                cwd=folder, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )) for name, prefix, arguments, status in cases]
            for name, status, process in processes:
                with self.subTest(module=name):
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, status, output + errors)
                    self.assertNotIn("ImportError", errors)
                    self.assertNotIn("ModuleNotFoundError", errors)

    def test_entry_scripts_load_each_shared_module_once(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        cases = (
            ("message/send.py", ("sessions.py", "relay_identity.py", "process_identity.py")),
            ("notify/auto_silence.py", ("../lib/user_zone.py",)),
            ("build_hold/build_hold.py", ("../lib/unit_runs.py",)),
            ("mac_test/audit.py", ("cargo_manifest.py",)),
        )
        source = """
import json
from pathlib import Path
import runpy
import sys

entry = Path(sys.argv[1])
watched = [Path(name).resolve() for name in sys.argv[2:]]
sys.argv = [str(entry), "--help"]
try:
    runpy.run_path(str(entry), run_name="__main__")
except SystemExit:
    pass
names = {str(path): [] for path in watched}
for name, module in list(sys.modules.items()):
    filename = getattr(module, "__file__", None)
    if isinstance(filename, str):
        path = str(Path(filename).resolve())
        if path in names:
            names[path].append([name, id(module)])
print("module_names " + json.dumps(names))
"""
        with tempfile.TemporaryDirectory() as temporary:
            environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            environment["HERDR_BIN"] = "/nonexistent/herdr"
            processes = [(entry, subprocess.Popen(
                [sys.executable, "-c", source, str(scripts / entry),
                 *(str((scripts / entry).parent / name) for name in watched)],
                cwd=temporary, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )) for entry, watched in cases]
            for entry, process in processes:
                with self.subTest(entry=entry):
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, 0, output + errors)
                    payload = next(line.removeprefix("module_names ") for line in output.splitlines()
                                   if line.startswith("module_names "))
                    names = cast(dict[str, list[tuple[str, int]]], json.loads(payload))
                    for filename, loaded in names.items():
                        with self.subTest(module=filename):
                            # An alias name for the same module object is one instance.
                            self.assertEqual(len({instance for _, instance in loaded}), 1, loaded)


if __name__ == "__main__":
    _ = unittest.main()
