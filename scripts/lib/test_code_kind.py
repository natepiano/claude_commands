"""The test-code naming rule used by repository scans."""
from pathlib import Path
import unittest

from scripts.lib.code_kind import is_test_code


class CodeKindTests(unittest.TestCase):
    def test_test_code_names_and_production_names(self) -> None:
        for name, expected in (
            ("scripts/lib/test_widget.py", True),
            ("scripts/tools/fake_widget.sh", True),
            ("scripts/tools/widget_test_support.py", True),
            ("scripts/lib/testing_tools.py", False),
            ("scripts/lib/support.py", False),
            ("scripts/test_fixtures/widget.py", False),
        ):
            with self.subTest(path=name):
                self.assertEqual(is_test_code(Path(name)), expected)


if __name__ == "__main__":
    _ = unittest.main()
