"""Tests for complete cells in tables that use native Markdown wrapping."""
from __future__ import annotations

import unittest

from scripts.production import display_cells


class WrappedTableTests(unittest.TestCase):
    def test_wrapped_rows_preserve_text_and_remove_only_empty_columns(self) -> None:
        headers = ("Unit", "Notes", "Workers", "Queued")
        for text in (
                "Review the complete production plan before delivery " * 8,
                "Review 界面 and cafe\u0301 before delivery " * 8,
                "界" * 180):
            with self.subTest(text=text):
                titles, rows = display_cells.wrapped(headers, [
                    ("one", text, "—", "—"),
                    ("two", "Check the result", "—", text),
                ])
                self.assertEqual(titles, ("Unit", "Notes", "Queued"))
                self.assertEqual(rows, [
                    ("one", text, "—"),
                    ("two", "Check the result", text),
                ])

    def test_wrapped_empty_table_keeps_its_headers(self) -> None:
        headers = ("Unit", "Notes", "Queued")
        self.assertEqual(display_cells.wrapped(headers, []), (headers, []))
