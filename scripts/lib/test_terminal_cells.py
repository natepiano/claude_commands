"""Terminal cell widths shared by queue lines and production reports."""
import unittest

from scripts.lib.terminal_cells import pad_left, pad_right, terminal_width


class TerminalCellsTests(unittest.TestCase):
    def test_queue_marks_and_unicode_names_use_terminal_cells(self) -> None:
        for text, cells in (("", 0), ("nextest", 7), ("■  1 ↑27", 8),
                            ("界語", 4), ("ＡＢ", 4), ("e\u0301", 1),
                            ("cafe\u0301 · 界", 9), ("🙂", 2)):
            with self.subTest(text=text):
                self.assertEqual(terminal_width(text), cells)

    def test_left_padding_uses_terminal_cells_and_preserves_long_text(self) -> None:
        for text, width, expected in (("abc", 5, "  abc"), ("界語", 6, "  界語"),
                                      ("e\u0301", 3, "  e\u0301"), ("界語", 3, "界語"),
                                      ("", 2, "  "), ("界", 2, "界"), ("", 0, ""),
                                      ("界7", 6, "   界7"), ("nextest", 3, "nextest")):
            with self.subTest(text=text, width=width):
                self.assertEqual(pad_left(text, width), expected)

    def test_right_padding_uses_terminal_cells_and_preserves_long_text(self) -> None:
        for text, width, expected in (("abc", 5, "abc  "), ("界語", 6, "界語  "),
                                      ("e\u0301", 3, "e\u0301  "), ("界語", 3, "界語"),
                                      ("", 2, "  "), ("界", 2, "界"), ("", 0, ""),
                                      ("界7", 6, "界7   "), ("nextest", 3, "nextest")):
            with self.subTest(text=text, width=width):
                self.assertEqual(pad_right(text, width), expected)


if __name__ == "__main__":
    _ = unittest.main()
