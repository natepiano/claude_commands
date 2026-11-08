"""Check package roots through copied interpreter shims."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class PythonRootTests(unittest.TestCase):
    def checkout(self, root: Path, value: str) -> Path:
        library = root / "scripts/lib"
        library.mkdir(parents=True)
        _ = shutil.copy2(Path(__file__).with_name("py"), library / "py")
        package = root / "scripts/example"
        package.mkdir()
        _ = (package / "value.py").write_text(f"VALUE = {value!r}\n")
        _ = (package / "entry.py").write_text(
            "import json, os\nfrom scripts.example.value import VALUE\n"
            + "print(json.dumps([VALUE, os.getcwd()]))\n")
        return library / "py"

    def run_shim(self, shim: Path, arguments: list[str], cwd: Path,
                 environment: dict[str, str], source: str = "") -> str:
        result = subprocess.run([str(shim), *arguments], cwd=cwd, env=environment,
                                input=source, capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_script_selects_its_checkout_from_each_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = self.checkout(root / "selected checkout", "selected")
            other = self.checkout(root / "other", "other")
            spaced = root / "working folder"
            spaced.mkdir()
            environment = {**os.environ, "PYTHONPATH": str(other.parents[2]),
                           "XDG_CACHE_HOME": str(root / "cache")}
            script = shim.parents[1] / "example/entry.py"
            for cwd in (Path("/"), Path("/tmp"), shim.parents[2], spaced):
                with self.subTest(cwd=cwd):
                    output = self.run_shim(other, ["-W", "ignore", str(script)], cwd, environment)
                    self.assertEqual(json.loads(output), ["selected", str(cwd)])

    def test_module_command_stdin_and_no_script_select_the_shim_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = self.checkout(root / "selected", "selected")
            other = self.checkout(root / "other", "other")
            environment = {**os.environ, "PYTHONPATH": str(other.parents[2]),
                           "XDG_CACHE_HOME": str(root / "cache")}
            source = "from scripts.example.value import VALUE; print(VALUE)"
            for arguments, stdin in ((["-m", "scripts.example.entry"], ""),
                                     (["-mscripts.example.entry"], ""),
                                     (["-c" + source], ""),
                                     (["-c", source], ""), (["-"], source), ([], source)):
                with self.subTest(arguments=arguments):
                    output = self.run_shim(shim, arguments, Path("/"), environment, stdin)
                    if arguments and arguments[0].startswith("-m"):
                        self.assertEqual(json.loads(output), ["selected", "/"])
                    else:
                        self.assertEqual(output, "selected")

    def test_bare_environment_loads_the_root_with_cold_and_cached_interpreters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = self.checkout(root / "selected", "selected")
            home = root / "home"
            home.mkdir()
            environment = {"PATH": os.environ["PATH"], "HOME": str(home)}
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    output = self.run_shim(shim, [str(shim.parents[1] / "example/entry.py")],
                                           Path("/tmp"), environment)
                    self.assertEqual(json.loads(output), ["selected", "/tmp"])

    def test_private_cache_loads_the_root_before_and_after_interpreter_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = self.checkout(root / "selected", "selected")
            environment = {**os.environ, "XDG_CACHE_HOME": str(root / "cache")}
            _ = environment.pop("CLAUDE_PYTHON", None)
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    output = self.run_shim(
                        shim, ["-c", "from scripts.example.value import VALUE; print(VALUE)"],
                        Path("/"), environment)
                    self.assertEqual(output, "selected")

    def test_inherited_startup_hook_remains_after_the_selected_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = self.checkout(root / "selected", "selected")
            hook = root / "hook"
            hook.mkdir()
            _ = (hook / "sitecustomize.py").write_text("import os; os.environ['ROOT_HOOK'] = 'loaded'\n")
            environment = {**os.environ, "PYTHONPATH": str(hook), "XDG_CACHE_HOME": str(root / "cache")}
            output = self.run_shim(shim, ["-c", "import os; print(os.environ['ROOT_HOOK'])"],
                                   Path("/"), environment)
            self.assertEqual(output, "loaded")

    def test_isolated_script_loads_the_root_without_inherited_paths_or_user_site(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = self.checkout(root / "selected", "selected")
            inherited = root / "inherited"
            inherited.mkdir()
            _ = (inherited / "foreign.py").write_text("raise RuntimeError('inherited path loaded')\n")
            script = shim.parents[1] / "example/isolated.py"
            _ = script.write_text(
                "import importlib.util, json, sys\nfrom scripts.example.value import VALUE\n"
                + "print(json.dumps([VALUE, importlib.util.find_spec('foreign'), sys.flags.no_user_site]))\n")
            environment = {**os.environ, "PYTHONPATH": str(inherited), "XDG_CACHE_HOME": str(root / "cache")}
            output = self.run_shim(shim, ["--isolated", str(script)], Path("/"), environment)
            self.assertEqual(json.loads(output), ["selected", None, 1])
