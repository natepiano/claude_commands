"""Tests for phase tables at the Claude Code width floor."""
from __future__ import annotations

import unittest

from scripts.production import claude_table_layout as layout


class ClaudeTableLayoutTests(unittest.TestCase):
    def test_fit_selects_full_proportional_and_hard_widths(self) -> None:
        headers = ("Unit", "Notes")
        rows = [("one", "alpha beta gamma")]
        for width, expected, hard in ((40, (4, 16), False), (25, (4, 10), False), (17, (3, 3), True)):
            with self.subTest(width=width):
                fitted = layout.fit(headers, rows, width)
                self.assertEqual(fitted.widths, expected)
                self.assertEqual(fitted.hard_wrap, hard)
        for heading, text, width, expected_lines in (
                ("Progress", "🟩" * 20, 55, 1),
                ("Workers", "impl waiting on run: Restart", 22, 5)):
            with self.subTest(heading=heading):
                fitted = layout.fit(("Unit", heading), [("one", text)], width)
                self.assertEqual(fitted.line_count(text, 1), expected_lines)
                self.assertFalse(fitted.hard_wrap)

    def test_long_queued_text_continues_without_losing_words(self) -> None:
        headers = ("Unit", "Notes", "Queued")
        queued = "Read " + "word " * 78 + "later"
        source = [("unit", "Review the current delivery and its tests", queued)]
        rows = layout.continued(headers, source, 107, 4, 30)
        self.assertGreater(len(rows), len(source))
        self.assertEqual(" ".join(row[2] for row in rows if row[2]), queued)
        self.assertTrue(all(row[0] == "unit" and row[1] == "" for row in rows[1:]))
        previous = (0,) * len(headers)
        for width in range(107, 220):
            fitted = layout.fit(headers, rows, width)
            self.assertTrue(fitted.keeps_table(headers, rows, width, 4))
            self.assertTrue(all(after >= before for before, after in zip(previous, fitted.widths, strict=True)))
            previous = fitted.widths

    def test_notes_prefer_item_marks_to_a_later_word_boundary(self) -> None:
        headers = ("Unit", "Notes", "Queued")
        first = "Review the plan"
        second = "verify the complete delivery with all current checks " * 6
        for mark in (" · ", "; "):
            with self.subTest(mark=mark):
                notes = first + mark + second.rstrip()
                rows = layout.continued(headers, [("unit", notes, "Review " * 60)], 65, 4, 30)
                self.assertEqual(rows[0][1], first + mark.rstrip())
                self.assertEqual(" ".join(row[1] for row in rows if row[1]), notes)
                fitted = layout.fit(headers, rows, 65)
                self.assertTrue(fitted.keeps_table(headers, rows, 65, 4))

    def test_long_ids_break_at_hyphens_without_wrapping_the_progress_bar(self) -> None:
        headers = ("Unit", "Status", "Progress", "Phase", "ETA Next", "ETA Plan", "Workers")
        source = [("admin-console", "busy", "🟩" * 20, "restart-record-replace", "no ETA stated",
                   "17:05 · phase gate-time-limit: Thu 19:00 · late 20h 32m",
                   "item137-bf9-final-callers working")]
        self.assertTrue(layout.fit(headers, source, 120).hard_wrap)
        rows = layout.continued(headers, source, 120, 4, 30)
        cells = [cell for row in rows for cell in row]
        for kept in ("no ETA stated", "17:05", "Thu 19:00", "admin-console"):
            with self.subTest(kept=kept):
                self.assertTrue(any(kept in cell for cell in cells))
        self.assertIn("restart- record- replace", rows[0][3])
        self.assertIn("item137- bf9- final- callers", " ".join(row[6] for row in rows))
        self.assertNotIn(" -", " ".join(cells))
        for width in (120, 138, 198):
            fitted = layout.fit(headers, rows, width)
            self.assertFalse(fitted.hard_wrap)
            self.assertEqual(fitted.line_count(rows[0][2], 2), 1)
            self.assertTrue(fitted.keeps_table(headers, rows, width, 4))

    def test_words_without_marks_break_from_the_break_width_or_before_a_hard_wrap(self) -> None:
        headers = ("Unit", "Progress", "Workers", "Queued")
        long_word = "x" * 60
        rows = layout.continued(headers, [("unit", "🟩" * 20, long_word, long_word)], 60, 4, 30)
        self.assertNotIn(long_word, [cell for row in rows for cell in row])
        plain = "verification" * 2
        identifier = "restart-record-replace-auto-silence"
        rows = layout.continued(("Unit", "Workers", "Queued"), [("unit", plain, identifier)], 62, 4, 30)
        self.assertEqual(rows[0][1], plain)
        self.assertNotIn(identifier, rows[0][2])
        rows = layout.continued(("Unit", "Workers"), [("unit", plain)], 30, 4, 30)
        self.assertEqual("".join(cell for row in rows for cell in row[1].split()), plain)
        self.assertNotIn(plain, [row[1] for row in rows])
        self.assertFalse(layout.fit(("Unit", "Workers"), rows, 30).hard_wrap)
