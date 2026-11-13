"""Check package roots through copied interpreter shims."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tarfile
import unittest

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


COPY_IMPORT_AUDIT = '''import json, os, sys
from pathlib import Path

def audit():
    root = Path(os.environ['EXPECTED_CHECKOUT']).resolve()
    paths = {}
    for name, module in list(sys.modules.items()):
        if name == 'scripts' or name.startswith('scripts.'):
            filename = getattr(module, '__file__', None)
            locations = [filename] if filename else list(getattr(module, '__path__', ()))
            paths[name] = locations
    print('IMPORTS=' + json.dumps(paths, sort_keys=True), flush=True)
    outside = {name: locations for name, locations in paths.items()
               if any(not Path(path).resolve().is_relative_to(root) for path in locations)}
    assert paths, 'No scripts modules were loaded'
    assert not outside, outside
'''
COPY_IMPORT_TEST = COPY_IMPORT_AUDIT + '''
import unittest
from scripts.build_targets import test_targets
from scripts.mac_test import test_cargo_manifest

class CopyImportsTests(unittest.TestCase):
    def test_imports(self):
        audit()

def load_tests(loader, tests, pattern):
    tests.addTests(loader.loadTestsFromModule(test_cargo_manifest))
    return tests
'''
COPY_TARGETED_RUN = COPY_IMPORT_AUDIT + '''
import runpy
from unittest import mock
from scripts.production import merge_gate
root = Path(os.environ['EXPECTED_CHECKOUT'])
def selected_suite(repo, *args, **kwargs):
    assert kwargs['mode'] is merge_gate.SuiteMode.TARGETED_TEST
    return merge_gate.run_suites(repo, machine=merge_gate.WholeMachine(),
        selected=merge_gate.SelectedModules(('scripts/build_targets/test_copy_imports.py',)),
        mode=kwargs['mode'])
sys.argv = [str(root / 'scripts/production/targeted_test.py'), '--repo', str(root)]
with mock.patch.object(merge_gate, 'run_gate_suites', side_effect=selected_suite):
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    finally:
        audit()
'''
COPY_DETACHED_RUN = '''import argparse, os, sys
from pathlib import Path
from unittest import mock
from scripts.agents import detached_runs

root = Path(os.environ['EXPECTED_CHECKOUT'])
record_path = root / 'detached.json'
record = dict(pid=os.getpid(), session_dir=str(root), worker='fixture-test',
              staff_id='fixture-staff', log=str(root / 'detached.log'),
              command=[sys.executable, *sys.argv[1:]])
# Simulate a supervisor that inherited the root from an older launcher.
os.environ['PYTHONPATH'] = str(Path(__file__).parent)
with (mock.patch.object(detached_runs, '_read_record', side_effect=lambda path: record.copy()),
      mock.patch.object(detached_runs, '_write_record', side_effect=lambda path, value: record.update(value) or value),
      mock.patch.object(detached_runs, '_finish_record'),
      mock.patch.object(detached_runs, 'deliver_in_fresh_process', return_value=0)):
    status = detached_runs.main(['supervise', str(record_path)])
print(Path(record['log']).read_text())
assert status == 0 and record['exit_code'] == 0, record
'''


class PythonRootTests(unittest.TestCase):
    def checkout(self, root: Path, value: str) -> Path:
        library = root / "scripts/lib"
        library.mkdir(parents=True)
        _ = shutil.copy2(Path(__file__).with_name("py"), library / "py")
        _ = shutil.copytree(Path(__file__).with_name("python_startup"), library / "python_startup",
                            ignore=shutil.ignore_patterns("__pycache__"))
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

    def test_old_revision_uses_its_own_scripts_with_the_new_shim(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            archive = folder / "HEAD.tar"
            _ = subprocess.run(["git", "archive", "847e0da3f", "-o", str(archive)],
                               cwd=repository, check=True, timeout=30)
            checkout = folder / "HEAD"
            checkout.mkdir()
            with tarfile.open(archive) as source:
                source.extractall(checkout, filter="data")
            self.assertFalse((checkout / "scripts/lib/python_startup/sitecustomize.py").exists())
            output = self.run_shim(repository / "scripts/lib/py", ["-c",
                "from scripts.build_targets import test_aws_status; print(test_aws_status.__file__)"],
                checkout, dict(os.environ))
            self.assertEqual(Path(output).resolve(), checkout / "scripts/build_targets/test_aws_status.py")

    def test_separator_preserves_a_filename_that_starts_with_a_dash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            shim = self.checkout(folder / "copy", "selected")
            _ = (folder / "-probe.py").write_text("import sys; print(repr(sys.argv))\n")
            for options in ([], ["-I"]):
                with self.subTest(options=options):
                    output = self.run_shim(shim, [*options, "--", "-probe.py", "payload"],
                                           folder, dict(os.environ))
                    self.assertEqual(output, "['-probe.py', 'payload']")

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
            other = self.checkout(root / "other", "other")
            hook = root / "hook"
            hook.mkdir()
            _ = (hook / "sitecustomize.py").write_text("import os; os.environ['ROOT_HOOK'] = 'loaded'\n")
            for paths in (str(hook), os.pathsep.join((str(other.parent / "python_startup"), str(hook)))):
                with self.subTest(inherited=paths):
                    environment = {**os.environ, "PYTHONPATH": paths, "XDG_CACHE_HOME": str(root / "cache")}
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

    def test_startup_hook_sees_native_arguments_and_children_inherit_no_checkout_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            shim = self.checkout(base / "copy", "selected")
            hook = base / "hook"
            hook.mkdir()
            records = base / "startup.jsonl"
            _ = (hook / "sitecustomize.py").write_text(
                "import json, os, sys\n"
                + "with open(os.environ['STARTUP_RECORDS'], 'a') as output:\n"
                + "    output.write(json.dumps(sys.argv) + '\\n')\n")
            entry = shim.parents[1] / "example/child.py"
            _ = entry.write_text(
                "import os, subprocess, sys\n"
                + "subprocess.run([sys.executable, '-c', \"import os; print(os.environ['PYTHONPATH'])\"], check=True)\n")
            environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(shim.parents[2]), str(hook))),
                           "STARTUP_RECORDS": str(records), "XDG_CACHE_HOME": str(base / "cache")}
            output = self.run_shim(shim, [str(entry), "payload"], shim.parents[2], environment)
            self.assertEqual(output, str(hook))
            self.assertEqual([json.loads(line) for line in records.read_text().splitlines()],
                             [[str(entry), "payload"], ["-c"]])

    def test_all_startup_hooks_select_only_the_child_checkout(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            parent, child = base / "A", base / "B"
            for checkout in (parent, child):
                _ = shutil.copytree(repository / "scripts", checkout / "scripts",
                                    ignore=shutil.ignore_patterns("__pycache__"))
                _ = shutil.copytree(repository / "config", checkout / "config")
            probe = "import json, scripts; print(json.dumps(list(scripts.__path__)))"
            _ = (child / "scripts/startup_probe.py").write_text(probe + "\n")
            repeat = (
                "import subprocess, sys, unittest; "
                "from scripts.message.repeat_guard_test_support import install_guard_test_budget; "
                "install_guard_test_budget(); "
                "subprocess.run([sys.argv[1] + '/scripts/lib/py', '-c', sys.argv[2]], cwd=sys.argv[1], check=True); "
                "unittest.doModuleCleanups()"
            )
            cargo = (
                "import subprocess, sys\n"
                "from scripts.lib.cargo_output_test_support import fake_cargo_output_roots\n"
                "with fake_cargo_output_roots(children=True):\n"
                "    subprocess.run([sys.executable, '-c', sys.argv[1]], cwd=sys.argv[2], check=True)\n"
            )
            cargo_probe = (
                "import gc; gc.collect()\n"
                + "from unittest.mock import Mock\n"
                + "from scripts.lib import cargo_output\n"
                + "assert isinstance(cargo_output.read_cargo_output_roots, Mock), 'Cargo hook released its mock'\n"
                + probe
            )
            environment = {**os.environ, "PYTHONPATH": "", "XDG_CACHE_HOME": str(base / "cache"),
                           "CONF_DIR": str(child / "config")}
            coverage = str(parent / "scripts/test_times/coverage_child")
            cases = (
                ("coverage command", ["-c", probe], coverage),
                ("coverage module", ["-m", "scripts.startup_probe"], coverage),
                ("repeat budget", ["-c", repeat, str(child), probe], ""),
                ("Cargo output", ["-c", cargo, cargo_probe, str(child)], ""),
            )
            for label, arguments, pythonpath in cases:
                with self.subTest(hook=label):
                    result = subprocess.run(
                        [str(parent / "scripts/lib/py"), *arguments],
                        cwd=child if label.startswith("coverage") else parent,
                        env={**environment, "PYTHONPATH": pythonpath}, capture_output=True,
                        text=True, timeout=TEST_HANG_GUARD_SECS,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(json.loads(result.stdout), [str(child / "scripts")])

    def test_failed_startup_hook_is_not_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            shim = self.checkout(base / "selected", "selected")
            hook = base / "hook"
            hook.mkdir()
            _ = (hook / "sitecustomize.py").write_text("raise RuntimeError('failed startup hook')\n")
            environment = {**os.environ, "PYTHONPATH": str(hook), "XDG_CACHE_HOME": str(base / "cache")}
            result = subprocess.run(
                [str(shim), "-c", "import sys; print(len(sys.modules['_devtools_checkout'].__dict__.get('startup_hooks', [])))"],
                cwd=base, env=environment, capture_output=True, text=True,
                timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("RuntimeError: failed startup hook", result.stderr)
            self.assertEqual(result.stdout.strip(), "0")

    def test_copy_imports_stay_in_the_copy(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            selected, other = base / "copy", base / "other"
            for checkout in (selected, other):
                _ = shutil.copytree(repository / "scripts", checkout / "scripts",
                                    ignore=shutil.ignore_patterns("__pycache__"))
                _ = (checkout / "scripts/build_targets/test_copy_imports.py").write_text(COPY_IMPORT_TEST)
            _ = shutil.copytree(repository / "config", selected / "config")
            wrapper = selected / "targeted_probe.py"
            _ = wrapper.write_text(COPY_TARGETED_RUN)
            detached = other / "detached_probe.py"
            _ = detached.write_text(COPY_DETACHED_RUN)
            environment = {
                **os.environ, "PYTHONPATH": os.pathsep.join((str(other), str(other), str(selected))),
                "EXPECTED_CHECKOUT": str(selected), "CONF_DIR": str(selected / "config"),
                "XDG_CACHE_HOME": str(base / "cache"), "BUILDLOG_SCOPE": "0",
            }
            for regular in (False, True):
                if regular:
                    for package in ('build_targets', 'mac_test'):
                        _ = (other / "scripts" / package / "__init__.py").write_text("")
                for arguments in (["-m", "unittest", "scripts.build_targets.test_copy_imports"], [str(wrapper)]):
                    for child in ("shim", "plain"):
                        with self.subTest(regular=regular, arguments=arguments, child=child):
                            launch = (["-c", "import os, sys; os.execv(sys.argv[1], sys.argv[1:])",
                                       str(selected / "scripts/lib/py"), *arguments] if child == "shim" else
                                      ["-c", "import os, sys; os.execv(sys.executable, [sys.executable, *sys.argv[1:]])",
                                       *arguments])
                            result = subprocess.run(
                                [str(other / "scripts/lib/py"), *launch], cwd=selected,
                                env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
                            )
                            self.assertIn("IMPORTS=", result.stdout)
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                with self.subTest(regular=regular, child="detached"):
                    result = subprocess.run(
                        [str(other / "scripts/lib/py"), str(detached),
                         "-m", "unittest", "scripts.build_targets.test_copy_imports"], cwd=selected,
                        env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
                    )
                    self.assertIn("IMPORTS=", result.stdout)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
