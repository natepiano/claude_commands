"""Terminal cell widths shared by queue, build-why and production reports."""
import unittest

from scripts.lib.terminal_cells import terminal_width


class TerminalCellsTests(unittest.TestCase):
    def test_queue_marks_and_unicode_names_use_terminal_cells(self) -> None:
        for text, cells in (("", 0), ("nextest", 7), ("■  1 ↑27", 8),
                            ("界語", 4), ("ＡＢ", 4), ("e\u0301", 1),
                            ("cafe\u0301 · 界", 9), ("🙂", 2)):
            with self.subTest(text=text):
                self.assertEqual(terminal_width(text), cells)


if __name__ == "__main__":
    _ = unittest.main()
