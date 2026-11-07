"""Check import conversion and shared module identity in a private checkout."""
from __future__ import annotations

import ast
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.lint import package_imports


class PackageImportTests(unittest.TestCase):
    def assert_conversion_refuses_dynamic_path(self, path_change: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts/alpha"
            folder.mkdir(parents=True)
            _ = (folder / "worker.py").write_text("VALUE = 'repository worker'\n")
            caller = folder / "caller.py"
            source = ('''#!/usr/bin/env python3
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
''' + path_change + '''
import worker
print(worker.VALUE)
''').encode()
            _ = caller.write_bytes(source)
            report = io.StringIO()
            with redirect_stdout(report):
                package_imports.convert(root, [caller])
            self.assertEqual(caller.read_bytes(), source)
            self.assertRegex(
                report.getvalue(),
                r"(?m)^scripts/alpha/caller\.py:[1-9][0-9]*: dynamic sys\.path change$",
            )

    def test_conversion_refuses_a_path_assignment_from_the_current_directory(self) -> None:
        self.assert_conversion_refuses_dynamic_path('sys.path[0] = os.getcwd()')

    def test_conversion_refuses_an_unsupported_file_directory_call(self) -> None:
        self.assert_conversion_refuses_dynamic_path('sys.path.insert(0, os.path.dirname(__file__))')

    def test_conversion_refuses_a_path_slice_with_a_vendor_directory(self) -> None:
        self.assert_conversion_refuses_dynamic_path('sys.path[:0] = [str(Path(__file__).parent), "/opt/vendor"]')

    def test_conversion_refuses_to_select_a_repository_import_after_a_plugin_path(self) -> None:
        self.assert_conversion_refuses_dynamic_path('sys.path.insert(0, os.environ["PLUGIN_DIR"])')

    def test_conversion_removes_an_extend_list_of_file_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts/alpha"
            folder.mkdir(parents=True)
            _ = (folder / "worker.py").write_text("VALUE = 'repository worker'\n")
            caller = folder / "caller.py"
            path_change = 'sys.path.extend([str(ROOT)])\n'
            source = ('''import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
''' + path_change + '''import worker
print(worker.VALUE, ROOT, sys.version_info.major)
''')
            _ = caller.write_bytes(source.encode())
            report = io.StringIO()
            with redirect_stdout(report):
                package_imports.convert(root, [caller])
            expected = source.replace(path_change, "", 1).replace("import worker\n", "from scripts.alpha import worker\n", 1)
            self.assertEqual(caller.read_bytes(), expected.encode())
            self.assertNotRegex(report.getvalue(), r"(?m)^scripts/alpha/caller\.py:[1-9][0-9]*:")

    def assert_conversion_refuses_source(self, source: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            caller = root / "scripts/alpha/caller.py"
            caller.parent.mkdir(parents=True)
            original = source.encode()
            _ = caller.write_bytes(original)
            report = io.StringIO()
            with redirect_stdout(report):
                package_imports.convert(root, [caller])
            self.assertEqual(caller.read_bytes(), original)
            self.assertRegex(report.getvalue(), r"(?m)^scripts/alpha/caller\.py:[1-9][0-9]*: .+$")

    def test_conversion_refuses_to_delete_a_function_that_has_a_caller(self) -> None:
        self.assert_conversion_refuses_source('''import sys
def configure():
    sys.path.insert(0, __file__)
configure()
''')

    def test_conversion_refuses_to_leave_a_decorator_without_its_class(self) -> None:
        self.assert_conversion_refuses_source('''import sys
def decorate(value):
    return value
@decorate
class Configured:
    sys.path.insert(0, __file__)
instance = Configured()
''')

    def test_conversion_refuses_a_path_change_on_a_line_with_a_kept_return(self) -> None:
        self.assert_conversion_refuses_source('''import sys
def configure(): sys.path.append(__file__); return 1
print(configure())
''')

    def test_conversion_refuses_binding_cleanup_that_would_empty_a_function(self) -> None:
        self.assert_conversion_refuses_source('''import sys
def configure():
    folder = __file__
    sys.path.append(folder)
configure()
''')

    def test_conversion_refuses_to_delete_a_loop_binding_read_after_the_loop(self) -> None:
        self.assert_conversion_refuses_source('''import sys
for directory in ("a",):
    sys.path.insert(0, __file__)
print(directory)
''')

    def test_conversion_keeps_a_refused_entry_unchanged_without_a_bootstrap(self) -> None:
        self.assert_conversion_refuses_source('''import sys
from scripts.alpha import worker
def configure():
    sys.path.insert(0, __file__)
if __name__ == "__main__":
    configure()
    print(worker.VALUE)
''')

    def test_conversion_removes_a_builtin_str_guard_for_a_resolved_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            caller = root / "scripts/alpha/caller.py"
            caller.parent.mkdir(parents=True)
            guard = '''    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
'''
            source = ('''import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def reviewer_environment():
''' + guard + '''    return 1
print(reviewer_environment(), ROOT, sys.version_info.major)
''')
            _ = caller.write_bytes(source.encode())
            report = io.StringIO()
            with redirect_stdout(report):
                package_imports.convert(root, [caller])
            converted = caller.read_bytes()
            _ = ast.parse(converted)
            self.assertEqual(converted, source.replace(guard, "", 1).encode())
            self.assertNotRegex(report.getvalue(), r"(?m)^scripts/alpha/caller\.py:[1-9][0-9]*:")

    def test_conversion_refuses_a_path_guard_that_calls_a_root_function(self) -> None:
        self.assert_conversion_refuses_source('''import sys
from pathlib import Path
def get_root():
    return Path(__file__).resolve().parents[2]
def reviewer_environment():
    if str(get_root()) not in sys.path:
        sys.path.insert(0, str(get_root()))
    return 1
print(reviewer_environment())
''')

    def test_conversion_refuses_a_path_guard_for_a_function_parameter(self) -> None:
        self.assert_conversion_refuses_source('''import sys
def reviewer_environment(value):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))
    return 1
print(reviewer_environment(__file__))
''')

    def test_conversion_refuses_a_path_guard_when_str_is_rebound(self) -> None:
        self.assert_conversion_refuses_source('''import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
str = repr
def reviewer_environment():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return 1
print(reviewer_environment())
''')

    def test_conversion_refuses_a_while_header_call_around_a_path_change(self) -> None:
        self.assert_conversion_refuses_source('''import sys
while str("x"): sys.path[:] = [__file__]
print("reached")
''')

    def test_conversion_removes_a_loop_whose_body_only_changes_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts/alpha"
            folder.mkdir(parents=True)
            caller = folder / "caller.py"
            loop = '''for directory in ("production", "shutdown"):
    sys.path.insert(0, __file__)
    sys.path.insert(0, __file__)
'''
            source = 'import sys\n# Keep this comment.\nVERSION = sys.version_info.major\n' + loop + '\nRESULT = "unchanged"  # Keep this spacing.\n'
            _ = caller.write_bytes(source.encode())
            with redirect_stdout(io.StringIO()):
                package_imports.convert(root, [caller])
            converted = caller.read_bytes()
            _ = ast.parse(converted)
            self.assertEqual(converted, source.replace(loop, "", 1).encode())

    def test_conversion_keeps_a_mixed_loop_when_a_nested_path_loop_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts/alpha"
            folder.mkdir(parents=True)
            caller = folder / "caller.py"
            path_change = '    sys.path.insert(0, __file__)\n'
            nested_loop = '''    for added in ("production", "shutdown"):
        sys.path.insert(0, __file__)
'''
            source = ('import sys\n# Keep the mixed loop.\nfor directory in ("alpha", "beta"):\n'
                      + path_change + nested_loop
                      + '    print(directory, sys.version_info.major)  # Keep this statement.\n')
            _ = caller.write_bytes(source.encode())
            with redirect_stdout(io.StringIO()):
                package_imports.convert(root, [caller])
            converted = caller.read_bytes()
            _ = ast.parse(converted)
            self.assertEqual(converted, source.replace(path_change, "", 1).replace(nested_loop, "", 1).encode())

    def test_conversion_keeps_the_file_when_path_removal_would_empty_one_if_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts/alpha"
            folder.mkdir(parents=True)
            caller = folder / "caller.py"
            source = '''import sys
if sys.version_info.major == 3:
    sys.path.insert(0, __file__)
else:
    print("Keep the other branch.")
'''
            _ = caller.write_bytes(source.encode())
            report = io.StringIO()
            with redirect_stdout(report):
                package_imports.convert(root, [caller])
            self.assertEqual(caller.read_bytes(), source.encode())
            self.assertRegex(report.getvalue(), r"(?m)^scripts/alpha/caller\.py:2: .+$")

    def test_bootstrap_preserves_the_docstring_future_imports_and_nested_entry_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = root / "scripts/lib/package_root.py"
            bootstrap.parent.mkdir(parents=True)
            _ = bootstrap.write_bytes((Path(__file__).resolve().parents[1] / "lib/package_root.py").read_bytes())
            source = '''#!/usr/bin/env python3
"""Keep the entry documentation.

Keep its second paragraph.
"""
from __future__ import annotations
from __future__ import division

from scripts.alpha import value
if __name__ == "__main__":
    print(__doc__, value.VALUE)
'''
            value = root / "scripts/alpha/value.py"
            value.parent.mkdir()
            _ = value.write_text("VALUE = 'loaded from the checkout'\n")
            old_line = 'with open(__file__.rpartition("/")[0] + "/../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})\n'
            for relative in ("alpha/entry.py", "alpha/nested/entry.py", "alpha/legacy.py"):
                with self.subTest(entry=relative):
                    entry = root / "scripts" / relative
                    entry.parent.mkdir(parents=True, exist_ok=True)
                    original = source.replace("\nfrom scripts.alpha", "\n" + old_line + "from scripts.alpha", 1) if relative == "alpha/legacy.py" else source
                    _ = entry.write_text(original)
                    self.assertTrue(package_imports.add_bootstrap(entry))
                    converted = entry.read_text()
                    lines = converted.splitlines(keepends=True)
                    position = next(index for index, line in enumerate(lines) if "/lib/package_root.py" in line)
                    inserted = "".join(lines[position - 1:position + 3])
                    self.assertEqual(converted.replace(inserted, "", 1), source)
                    self.assertEqual(entry.read_bytes().replace(inserted.encode(), b"", 1), source.encode())
                    self.assertNotIn(old_line, converted)
                    self.assertLess(converted.index("from __future__ import division"), converted.index(inserted))
                    self.assertLess(converted.index(inserted), converted.index("from scripts.alpha import value"))
                    self.assertFalse(package_imports.add_bootstrap(entry))
                    self.assertEqual(entry.read_text(), converted)
                    result = subprocess.run(
                        [sys.executable, str(entry)], cwd=root.parent,
                        env={name: value for name, value in os.environ.items() if name != "PYTHONPATH"},
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "Keep the entry documentation.\n\nKeep its second paragraph.\n loaded from the checkout\n")

    def test_bootstrap_leaves_tests_and_modules_without_an_entry_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "scripts/alpha"
            folder.mkdir(parents=True)
            sources = {
                "test_entry.py": '#!/usr/bin/env python3\nif __name__ == "__main__":\n    print("test")\n',
                "library.py": 'VALUE = "library"\n',
                "standalone.py": '#!/usr/bin/env python3\nimport sys\nif __name__ == "__main__":\n    print(sys.version)\n',
            }
            for name, source in sources.items():
                with self.subTest(file=name):
                    path = folder / name
                    _ = path.write_text(source)
                    self.assertFalse(package_imports.add_bootstrap(path))
                    self.assertEqual(path.read_text(), source)
                    self.assertEqual(path.read_bytes(), source.encode())

    def test_conversion_makes_each_entry_importable_under_bare_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts/alpha"
            folder.mkdir(parents=True)
            bootstrap = root / "scripts/lib/package_root.py"
            bootstrap.parent.mkdir()
            _ = bootstrap.write_bytes((Path(__file__).resolve().parents[1] / "lib/package_root.py").read_bytes())
            _ = (folder / "worker.py").write_text("VALUE = 'converted entry'\n")
            entries = (folder / "shebang.py", folder / "main.py")
            _ = entries[0].write_text('#!/usr/bin/env python3\nimport worker\nprint(worker.VALUE)\n')
            _ = entries[1].write_text('import worker\nif __name__ == "__main__":\n    print(worker.VALUE)\n')
            with redirect_stdout(io.StringIO()):
                package_imports.convert(root, [folder])
            for entry in entries:
                with self.subTest(entry=entry.name):
                    result = subprocess.run(
                        [sys.executable, str(entry)], cwd=root.parent,
                        env={name: value for name, value in os.environ.items() if name != "PYTHONPATH"},
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "converted entry\n")

    def test_conversion_bootstraps_an_entry_with_only_a_dynamic_sibling_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts/alpha"
            folder.mkdir(parents=True)
            bootstrap = root / "scripts/lib/package_root.py"
            bootstrap.parent.mkdir()
            _ = bootstrap.write_bytes((Path(__file__).resolve().parents[1] / "lib/package_root.py").read_bytes())
            _ = (folder / "worker.py").write_text("VALUE = 'dynamic entry'\n")
            entry = folder / "entry.py"
            _ = entry.write_text('#!/usr/bin/env python3\nworker = __import__("worker")\nprint(worker.VALUE)\n')
            with redirect_stdout(io.StringIO()):
                package_imports.convert(root, [folder])
            result = subprocess.run(
                [sys.executable, str(entry)], cwd=root.parent,
                env={name: value for name, value in os.environ.items() if name != "PYTHONPATH"},
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "dynamic entry\n")

    def test_conversion_rerun_bootstraps_an_unchanged_caller_outside_the_selected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts/alpha"
            folder.mkdir(parents=True)
            bootstrap = root / "scripts/lib/package_root.py"
            bootstrap.parent.mkdir()
            _ = bootstrap.write_bytes((Path(__file__).resolve().parents[1] / "lib/package_root.py").read_bytes())
            _ = (folder / "worker.py").write_text("VALUE = 'outside caller'\n")
            with redirect_stdout(io.StringIO()):
                package_imports.convert(root, [folder])
            entry = root / "scripts/beta/caller.py"
            entry.parent.mkdir()
            _ = entry.write_text('#!/usr/bin/env python3\nfrom scripts.alpha import worker\nprint(worker.VALUE)\n')
            with redirect_stdout(io.StringIO()):
                package_imports.convert(root, [folder])
            result = subprocess.run(
                [sys.executable, str(entry)], cwd=root.parent,
                env={name: value for name, value in os.environ.items() if name != "PYTHONPATH"},
                capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "outside caller\n")

    def test_conversion_keeps_comments_aliases_and_one_module_for_patches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alpha, beta = root / "scripts/alpha", root / "scripts/beta"
            alpha.mkdir(parents=True)
            beta.mkdir()
            bootstrap = root / "scripts/lib/package_root.py"
            bootstrap.parent.mkdir()
            _ = bootstrap.write_bytes((Path(__file__).resolve().parents[1] / "lib/package_root.py").read_bytes())
            sources = {
                alpha / "worker.py": "def result():\n    return 'original'\n",
                alpha / "caller.py": '''#!/usr/bin/env python3
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.append(str(Path(__file__).resolve().parent))
sys.path = [str(ROOT)]
sys.path[:0] = [str(ROOT)]
import worker as peer  # Keep this comment.
from .worker import result
from ..alpha import worker
from unittest.mock import patch
TARGET = "worker.result"
def check():
    with patch(TARGET, return_value='patched'):
        return peer.result(), worker.result()
''',
                beta / "consumer.py": '''from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "alpha"))
import worker
from worker import result
from unittest.mock import patch
def check():
    with patch("worker.result", return_value='outside'):
        return worker.result()
''',
            }
            for path, source in sources.items():
                _ = path.write_text(source)
            report = io.StringIO()
            with redirect_stdout(report):
                package_imports.convert(root, [alpha])
            converted = (alpha / "caller.py").read_text()
            self.assertIn("# Keep this comment.", converted)
            self.assertNotIn("sys.path", converted)
            self.assertNotIn("import sys", converted)
            self.assertNotIn("from pathlib import Path", converted)
            self.assertNotIn("ROOT =", converted)
            self.assertTrue(converted.startswith("#!/usr/bin/env python3\n"))
            self.assertIn("outside: scripts/beta/consumer.py", report.getvalue())
            self.assertIn("sys.path.insert", (beta / "consumer.py").read_text())
            result = subprocess.run([sys.executable, "-c", '''from scripts.alpha import caller
from scripts.beta import consumer
print(caller.check(), consumer.check())
'''], cwd=root, env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "('patched', 'patched') outside")
            before = {path: path.read_bytes() for path in sources}
            with redirect_stdout(io.StringIO()):
                package_imports.convert(root, [alpha])
            self.assertEqual({path: path.read_bytes() for path in sources}, before)

    def test_an_ambiguous_import_stays_and_reports_its_source_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("alpha", "beta"):
                path = root / "scripts" / folder
                path.mkdir(parents=True)
                _ = (path / "shared.py").write_text("VALUE = 1\n")
            caller = root / "scripts/caller.py"
            _ = caller.write_text("import shared\n")
            report = io.StringIO()
            with redirect_stdout(report):
                package_imports.convert(root, [caller])
            self.assertIn("scripts/caller.py:1: ambiguous module shared", report.getvalue())
            self.assertEqual(caller.read_text(), "import shared\n")

    def test_a_shebang_inside_a_string_keeps_its_runtime_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts/alpha"
            folder.mkdir(parents=True)
            caller = folder / "caller.py"
            _ = caller.write_text('print("""#!/usr/bin/env python3\n""", end="")\n')
            with redirect_stdout(io.StringIO()):
                package_imports.convert(root, [caller])
            result = subprocess.run([sys.executable, str(caller)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "#!/usr/bin/env python3\n")

    def test_string_imports_share_the_module_and_dynamic_names_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "scripts/alpha"
            folder.mkdir(parents=True)
            _ = (folder / "worker.py").write_text("VALUE = 'worker'\n")
            caller = folder / "caller.py"
            _ = caller.write_text('''import sys, importlib
sys.path.append(__file__)
def load(name):
    return importlib.import_module(name)
peer = importlib.import_module("worker")
other = __import__("worker")
print(peer is other, other.VALUE)
''')
            report = io.StringIO()
            with redirect_stdout(report):
                package_imports.convert(root, [folder])
            self.assertIn("scripts/alpha/caller.py:4: dynamic module name", report.getvalue())
            self.assertNotIn("sys", caller.read_text())
            result = subprocess.run([sys.executable, "-m", "scripts.alpha.caller"], cwd=root,
                                    env={**os.environ, "PYTHONPATH": str(root)},
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "True worker")

    def test_the_own_folder_precedes_an_added_folder_for_duplicate_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("alpha", "beta"):
                path = root / "scripts" / folder
                path.mkdir(parents=True)
                _ = (path / "shared.py").write_text(f"VALUE = {folder!r}\n")
            caller = root / "scripts/alpha/caller.py"
            _ = caller.write_text('''import sys
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "beta")]
import shared
''')
            entry = root / "scripts/entry.py"
            _ = entry.write_text('''import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent / "beta"))
import shared
''')
            with redirect_stdout(io.StringIO()):
                package_imports.convert(root, [caller, entry])
            result = subprocess.run([sys.executable, "-c", '''from scripts.alpha import caller
from scripts import entry
print(caller.shared.VALUE, entry.shared.VALUE)
'''], cwd=root, env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "alpha beta")


if __name__ == "__main__":
    _ = unittest.main()
