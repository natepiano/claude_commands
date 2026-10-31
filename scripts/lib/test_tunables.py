"""Configuration is read at use and missing settings never become defaults."""
from pathlib import Path
import tempfile
from typing import override
import unittest

from scripts.lib import tunables


class TunablesTests(unittest.TestCase):
    config: Path = Path()

    @override
    def setUp(self) -> None:
        self.config = Path(self.enterContext(tempfile.TemporaryDirectory())) / "direct.conf"

    def test_values_parse_comments_whitespace_and_equals_without_caching(self) -> None:
        _ = self.config.write_text("# comment\n LIMIT = 12 # current value\nEMPTY=\nPATH=a=b\nignored\n")
        self.assertEqual(tunables.values(self.config), {"LIMIT": "12", "EMPTY": "", "PATH": "a=b"})
        _ = self.config.write_text("LIMIT=7\n")
        self.assertEqual(tunables.values(self.config), {"LIMIT": "7"})

    def test_number_reads_changed_values_and_accepts_both_bounds(self) -> None:
        for value in ("1", "2.5", "4"):
            _ = self.config.write_text(f"LIMIT={value}\n")
            self.assertEqual(tunables.number(self.config, "LIMIT", minimum=1, maximum=4), float(value))

    def test_missing_file_missing_key_and_bad_numbers_name_file_and_key(self) -> None:
        for content in (None, "OTHER=2\n", "LIMIT=bad\n", "LIMIT=nan\n", "LIMIT=inf\n",
                        "LIMIT=0\n", "LIMIT=5\n", "LIMIT=\n"):
            with self.subTest(content=content):
                if content is not None:
                    _ = self.config.write_text(content)
                with self.assertRaises(tunables.TunableUnusable) as caught:
                    _ = tunables.number(self.config, "LIMIT", minimum=1, maximum=4)
                self.assertIn(str(self.config), str(caught.exception))
                self.assertIn("LIMIT", str(caught.exception))

    def test_unreadable_values_report_the_path_without_supplying_an_empty_config(self) -> None:
        _ = self.config.write_bytes(b"LIMIT=\xff")
        with self.assertRaises(tunables.TunableUnusable) as caught:
            _ = tunables.values(self.config)
        self.assertIn(str(self.config), str(caught.exception))
