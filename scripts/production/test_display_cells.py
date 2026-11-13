"""Tests for complete cells in tables that use native Markdown wrapping."""
from __future__ import annotations

import unittest

from scripts.lib import conf
from scripts.production import claude_table_layout, display_cells


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
                for name, originals in (("one", (text, "—")), ("two", ("Check the result", text))):
                    selected = [row for row in rows if row[0] == name]
                    for column, original in enumerate(originals, 1):
                        restored = " ".join(row[column] for row in selected if row[column])
                        self.assertEqual(restored.replace(" ", ""), original.replace(" ", ""))
                settings = conf.read_or_repository("production")
                width = settings.whole("units_table_floor_columns") - claude_table_layout.REPLY_INDENT
                fitted = claude_table_layout.fit(titles, rows, width)
                self.assertTrue(fitted.keeps_table(titles, rows, width, settings.whole("units_table_cell_lines")))

    def test_wrapped_empty_table_keeps_its_headers(self) -> None:
        headers = ("Unit", "Notes", "Queued")
        self.assertEqual(display_cells.wrapped(headers, []), (headers, []))
