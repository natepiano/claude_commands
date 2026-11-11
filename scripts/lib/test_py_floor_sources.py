"""Check the syntax floor in scans of the repository's own scripts."""

import ast
from pathlib import Path
import unittest

from scripts.lib.py_floor_test_support import floor


class PythonFloorTests(unittest.TestCase):
    def test_every_script_parses_at_the_interpreter_floor(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        version = floor()
        failures: list[str] = []
        for path in sorted(scripts.rglob("*.py")):
            try:
                _ = ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=version)
            except SyntaxError as error:
                failures.append(f"{path.relative_to(scripts.parent)}: {error}")
        self.assertEqual(failures, [], "Scripts exceed the interpreter floor:\n" + "\n".join(failures))


if __name__ == "__main__":
    _ = unittest.main()
