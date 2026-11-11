"""Terminal cell widths shared by queue lines and production reports."""
from contextlib import redirect_stdout
import io
import unittest

from scripts.lib.terminal_cells import pad_left, pad_right, print_table, terminal_width


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

    def test_tables_align_number_columns_and_keep_text_columns_left_aligned(self) -> None:
        cases = (
            (("amount",), (("12",), ("3",)), "amount\n    12\n     3\n"),
            (("amount",), (("$2",), ("$123.45",)), " amount\n     $2\n$123.45\n"),
            (("amount",), (("+2%",), ("-12.5%",)), "amount\n   +2%\n-12.5%\n"),
            (("amount",), (("1s",), ("12.5s",)), "amount\n    1s\n 12.5s\n"),
            (("amount",), (("-$2.5",), ("$+3",)), "amount\n -$2.5\n   $+3\n"),
            (("amount",), ((".5",), ("1e+3",)), "amount\n    .5\n  1e+3\n"),
            (("amount",), (("—",), ("1",)), "amount\n     —\n     1\n"),
            (("amount",), (("—",), ("—",)), "amount\n     —\n     —\n"),
            (("amount",), (("1234567",), ("none",)), "amount\n1234567\nnone\n"),
            (("amount",), (("1",), ("1/2",)), "amount\n1\n1/2\n"),
            (("amount",), (("1",), ("2026-11-11",)), "amount\n1\n2026-11-11\n"),
            (("amount",), (("1",), ("2 sec",)), "amount\n1\n2 sec\n"),
            (("amount",), (("1",), ("",)), "amount\n1\n\n"),
            (("名", "CPU"), (("界", "1"), ("e\u0301", "12")), "名  CPU\n界    1\ne\u0301    12\n"),
        )
        for headers, rows, expected in cases:
            with self.subTest(rows=rows):
                output = io.StringIO()
                with redirect_stdout(output):
                    print_table(headers, rows)
                self.assertEqual(output.getvalue(), expected)


if __name__ == "__main__":
    _ = unittest.main()
