"""Check package bootstrap insertion, tree scans, and entry execution."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.lint import package_imports


class PackageImportTests(unittest.TestCase):
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
            same_folder_block = '''try:
    with open(__file__[:__file__.rfind("/") + 1] + "package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass
'''
            for relative, existing_block in (
                ("alpha/entry.py", ""),
                ("alpha/nested/entry.py", ""),
                ("alpha/legacy.py", old_line),
                ("lib/entry.py", same_folder_block),
            ):
                with self.subTest(entry=relative):
                    entry = root / "scripts" / relative
                    entry.parent.mkdir(parents=True, exist_ok=True)
                    original = source.replace("\nfrom scripts.alpha", "\n" + existing_block + "from scripts.alpha", 1)
                    _ = entry.write_text(original)
                    self.assertTrue(package_imports.add_bootstrap(entry))
                    converted = entry.read_text()
                    lines = converted.splitlines(keepends=True)
                    position = next(index for index, line in enumerate(lines) if "/lib/package_root.py" in line)
                    inserted = "".join(lines[position - 1:position + 3])
                    self.assertEqual(converted.replace(inserted, "", 1), source)
                    self.assertEqual(entry.read_bytes().replace(inserted.encode(), b"", 1), source.encode())
                    self.assertNotIn(old_line, converted)
                    self.assertEqual(converted.count("package_root.py"), 1)
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

            read_statement = 'with open(__file__[:__file__.rfind("/") + 1] + "package_root.py") as _package_root: contents = _package_root.read()\n'
            entries = {
                "alpha/shebang.py": '#!/usr/bin/env python3\nfrom scripts.alpha import value\nprint(value.VALUE)\n',
                "alpha/main.py": 'from scripts.alpha import value\nif __name__ == "__main__":\n    print(value.VALUE)\n',
                "alpha/dynamic.py": '#!/usr/bin/env python3\nvalue = __import__("scripts.alpha.value", fromlist=["VALUE"])\nprint(value.VALUE)\n',
                "beta/caller.py": '#!/usr/bin/env python3\nfrom scripts.alpha import value\nprint(value.VALUE)\n',
                "lib/reader.py": '#!/usr/bin/env python3\n' + read_statement + 'from scripts.alpha import value\nprint(value.VALUE if contents else "empty bootstrap")\n',
            }
            for relative, original in entries.items():
                with self.subTest(entry=relative):
                    entry = root / "scripts" / relative
                    entry.parent.mkdir(parents=True, exist_ok=True)
                    _ = entry.write_text(original)
                    for count in (1, 0):
                        command = subprocess.run(
                            [sys.executable, str(Path(package_imports.__file__).resolve()), "bootstrap", str(root)],
                            cwd=root.parent,
                            env={name: value for name, value in os.environ.items() if name != "PYTHONPATH"},
                            capture_output=True, text=True, check=False, timeout=30,
                        )
                        self.assertEqual(command.returncode, 0, command.stderr)
                        self.assertEqual(command.stdout, f"bootstrap: {count} entry files\n")
                    if relative == "lib/reader.py":
                        converted = entry.read_text()
                        self.assertIn(read_statement, converted)
                        self.assertEqual(converted.count("as _package_root: exec("), 1)
                    result = subprocess.run(
                        [sys.executable, str(entry)], cwd=root.parent,
                        env={name: value for name, value in os.environ.items() if name != "PYTHONPATH"},
                        capture_output=True, text=True, check=False, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "loaded from the checkout\n")

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


if __name__ == "__main__":
    _ = unittest.main()
