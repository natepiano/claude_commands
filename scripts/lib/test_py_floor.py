"""Check the interpreter floor against syntax and the real finder."""

import ast
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.lib.py_floor_test_support import cache_file_name, floor
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


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

    def test_a_cold_cache_records_an_absolute_interpreter_path(self) -> None:
        finder = Path(__file__).with_name("py")
        with tempfile.TemporaryDirectory() as temporary:
            environment = {**os.environ, "XDG_CACHE_HOME": temporary}
            _ = environment.pop("CLAUDE_PYTHON", None)
            result = subprocess.run(
                ["bash", str(finder), "-c", "pass"], env=environment,
                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            cached = Path(temporary) / "claude" / cache_file_name()
            interpreter = Path(cached.read_text(encoding="utf-8").strip())
            self.assertTrue(interpreter.is_absolute(), str(interpreter))
            self.assertTrue(interpreter.is_file(), str(interpreter))

    def test_a_missing_minor_assignment_has_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            finder = Path(temporary) / "py"
            _ = finder.write_text("NEED_MAJOR=3\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, rf"{finder}:.*NEED_MINOR"):
                _ = floor(finder)

    def test_a_duplicate_minor_assignment_with_a_comment_has_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            finder = Path(temporary) / "py"
            _ = finder.write_text(
                "NEED_MAJOR=3\nNEED_MINOR=12\nNEED_MINOR=13 # updated floor\n", encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, rf"{finder}: expected exactly one NEED_MINOR=<integer> line"):
                _ = floor(finder)

    def test_a_noninteger_minor_assignment_has_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            finder = Path(temporary) / "py"
            _ = finder.write_text("NEED_MAJOR=3\nNEED_MINOR=twelve\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, rf"{finder}: expected exactly one NEED_MINOR=<integer> line"):
                _ = floor(finder)

    def test_a_comment_glued_to_the_minor_digits_has_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            finder = Path(temporary) / "py"
            _ = finder.write_text("NEED_MAJOR=3\nNEED_MINOR=12#floor\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, rf"{finder}: expected exactly one NEED_MINOR=<integer> line"):
                _ = floor(finder)

    def test_a_minor_assignment_accepts_a_trailing_comment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            finder = Path(temporary) / "py"
            _ = finder.write_text("NEED_MAJOR=3\nNEED_MINOR=12 # floor\n", encoding="utf-8")
            self.assertEqual(floor(finder), (3, 12))
