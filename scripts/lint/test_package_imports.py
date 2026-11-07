"""Check import conversion and shared module identity in a private checkout."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.lint.package_imports import convert


class PackageImportTests(unittest.TestCase):
    def test_conversion_keeps_comments_aliases_and_one_module_for_patches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alpha, beta = root / "scripts/alpha", root / "scripts/beta"
            alpha.mkdir(parents=True)
            beta.mkdir()
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
                convert(root, [alpha])
            converted = (alpha / "caller.py").read_text()
            self.assertIn("# Keep this comment.", converted)
            self.assertNotIn("sys.path", converted)
            self.assertNotIn("import sys", converted)
            self.assertNotIn("from pathlib import Path", converted)
            self.assertNotIn("ROOT =", converted)
            self.assertTrue(converted.startswith("#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py\n"))
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
                convert(root, [alpha])
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
                convert(root, [caller])
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
                convert(root, [caller])
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
                convert(root, [folder])
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
                convert(root, [caller, entry])
            result = subprocess.run([sys.executable, "-c", '''from scripts.alpha import caller
from scripts import entry
print(caller.shared.VALUE, entry.shared.VALUE)
'''], cwd=root, env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "alpha beta")


if __name__ == "__main__":
    _ = unittest.main()
