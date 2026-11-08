"""A loaded file can use its registered module during execution."""
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

from scripts.lib.file_module import load_file_module


class FileModuleTests(unittest.TestCase):
    def test_file_sees_its_registered_module_before_execution_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hyphenated-file.py"
            name = "file_module_fixture"
            self.addCleanup(sys.modules.pop, name, None)
            _ = path.write_text("import sys\nregistered = sys.modules[__name__]\n")
            module = load_file_module(name, path)
            self.assertIs(cast(dict[str, object], vars(module))["registered"], module)
            self.assertIs(sys.modules[name], module)
