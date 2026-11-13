"""The queue's text and pane share cell-aware lines and span styling."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from typing import cast
import unittest
from unittest import mock

from scripts.build_queue.queue_lines import (
    Line,
    age,
    Span,
    clip,
    cut_tail,
    json_form,
    plain,
    step_prose,
    step_text,
    wrap,
)
from scripts.lib.terminal_cells import terminal_width


def styled_letters(lines: Sequence[Line]) -> list[tuple[str, str, bool]]:
    """Ignore wrap whitespace while retaining the style of every visible letter."""
    return [(letter, part.tone, part.bold)
            for line in lines for part in line for letter in part.text if not letter.isspace()]


class StepAgeTests(unittest.TestCase):
    def test_minutes_floor_elapsed_time_and_never_show_a_negative_wait(self) -> None:
        for elapsed, expected in ((-1, 0), (0, 0), (59.9, 0), (60, 1), (125, 2)):
            with self.subTest(elapsed=elapsed):
                self.assertEqual(age(1000 - elapsed, 1000), expected)


class StepNameTests(unittest.TestCase):
    def test_recorded_units_replace_repository_names_in_step_text_and_prose(self) -> None:
        from scripts.build_queue import admission_records as records
        from scripts.lib.build_steps import StepNames
        from scripts.message import unit_labels
        from scripts.production.staff_test_support import fixture_member, isolated_staff_file

        with tempfile.TemporaryDirectory() as directory, isolated_staff_file(Path(directory) / "staff.json", {}):
            runner = fixture_member("showrunner", name="tool-based-ui")
            unit = fixture_member("unit", name="trunk", parent=runner.id)
            solo = fixture_member("producer", name="solo")
            with mock.patch.object(unit_labels, "newest_sessions", return_value=[]):
                labels = unit_labels.unit_labels_for([unit.current_session, solo.current_session], {})
            names = StepNames({"names.check": "check", "prose.check": "type check"})
            waiter = records.WaitingStep(123, unit.current_session, "trunk", "check", "/private/tree",
                                         0, 1, records.ReadyForAdmission(), project="tool-based-ui-trunk")
            running = records.RunningStep(unit.current_session, "trunk", "check", "/private/tree", 1,
                                           records.PromisedMemory(1), pid=123, project="tool-based-ui-trunk")
            for step in (waiter, running):
                for session, saved_unit, expected in ((unit.current_session, "", "trunk"),
                        (solo.current_session, "", "tool-based-ui-trunk"),
                        ("unrecorded", "", "tool-based-ui-trunk"),
                        ("unrecorded", "saved-unit", "saved-unit"),
                        (unit.current_session, "saved-unit", "saved-unit")):
                    with self.subTest(step=type(step).__name__, session=session, unit=saved_unit):
                        row = replace(step, session_id=session, unit=saved_unit)
                        self.assertEqual(step_text(row, names, labels=labels), expected + " check")
                        self.assertEqual(step_prose(row, names, labels=labels), expected + " type check")


class CellWidthTests(unittest.TestCase):
    def test_cut_tail_reserves_a_cell_for_the_ellipsis_only_on_overflow(self) -> None:
        for text, cells, expected in (("worker", 6, "worker"), ("worker", 8, "worker"),
                                      ("worker", 4, "wor…"), ("worker", 1, "…"),
                                      ("worker", 0, ""), ("", 3, ""),
                                      ("界語z", 4, "界…"), ("界語", 4, "界語"),
                                      ("界語", 2, "…"), ("Cafe\u0301界!", 5, "Cafe\u0301…")):
            with self.subTest(text=text, cells=cells):
                self.assertEqual(cut_tail(text, cells), expected)
                self.assertLessEqual(terminal_width(cut_tail(text, cells)), cells)


class WrappingTests(unittest.TestCase):
    def test_one_style_wraps_into_one_whole_span_per_row(self) -> None:
        line = (Span("alpha beta ", "warning", True), Span("gamma delta", "warning", True))
        rows = wrap(line, 11, indent=0)
        self.assertEqual(rows, [(Span("alpha beta", "warning", True),),
                                (Span("gamma delta", "warning", True),)])

    def test_wide_word_separators_never_exceed_the_cell_budget(self) -> None:
        line = (Span("a\u3000b", "warning", True),)
        lines = wrap(line, 3)
        self.assertTrue(all(terminal_width(plain([row])) <= 3 for row in lines))
        self.assertEqual(styled_letters(lines), styled_letters([line]))

    def test_newlines_force_breaks_without_remaining_inside_a_row(self) -> None:
        line = (Span("alpha\nbeta", "error", True), Span("\n", "inactive"),
                Span("gamma", "success"))
        lines = wrap(line, 40)
        self.assertEqual([plain([row]).strip() for row in lines], ["alpha", "beta", "gamma"])
        self.assertTrue(all("\n" not in part.text for row in lines for part in row))
        self.assertEqual(styled_letters(lines), styled_letters([line]))

    def test_word_wrap_uses_the_requested_hanging_indent_and_retains_styles(self) -> None:
        line = (Span("Waits on ", "default", True), Span("memory pressure", "error"),
                Span(" and room", "warning"))
        lines = wrap(line, 20, indent=4)
        self.assertEqual(plain(lines), "Waits on memory\n    pressure and\n    room")
        self.assertEqual(styled_letters(lines), styled_letters([line]))
        self.assertTrue(all(terminal_width(plain([row])) <= 20 for row in lines))

    def test_a_word_split_across_spans_stays_together_at_a_wrap_boundary(self) -> None:
        line = (Span("one "), Span("mem", "error", True), Span("ory", "warning"),
                Span(" two", "inactive"))
        lines = wrap(line, 9, indent=2)
        self.assertEqual(plain(lines), "one\n  memory\n  two")
        self.assertEqual(styled_letters(lines), styled_letters([line]))

    def test_long_words_split_to_fit_instead_of_exceeding_the_columns(self) -> None:
        line = (Span("abcdefghijkl", "inactive", True),)
        lines = wrap(line, 6, indent=2)
        self.assertEqual(plain(lines), "abcdef\n  ghij\n  kl")
        self.assertEqual(styled_letters(lines), styled_letters([line]))

    def test_wide_and_combining_characters_wrap_by_cells(self) -> None:
        line = (Span("界語 ", "success"), Span("e\u0301clair fin", "error", True))
        lines = wrap(line, 8, indent=2)
        self.assertEqual(plain(lines), "界語\n  e\u0301clair\n  fin")
        self.assertEqual(styled_letters(lines), styled_letters([line]))
        self.assertTrue(all(terminal_width(plain([row])) <= 8 for row in lines))

    def test_default_hanging_indent_and_empty_lines(self) -> None:
        self.assertEqual(plain(wrap((Span("alpha beta gamma"),), 10)), "alpha beta\n  gamma")
        self.assertEqual(plain(wrap((Span("alpha beta"),), 10)), "alpha beta")
        self.assertEqual(len(wrap((), 10)), 1)
        self.assertEqual(plain(wrap((), 10)), "")

    def test_one_cell_lines_make_progress_even_when_indent_cannot_fit(self) -> None:
        line = (Span("abc", "warning", True),)
        lines = wrap(line, 1, indent=4)
        self.assertEqual(plain(lines), "a\nb\nc")
        self.assertEqual(styled_letters(lines), styled_letters([line]))


class ClippingTests(unittest.TestCase):
    def test_a_line_within_the_budget_keeps_every_span_and_style(self) -> None:
        line = (Span("■ ", "error", True), Span("界e\u0301", "inactive"))
        self.assertEqual(clip(line, 8), line)
        self.assertEqual(clip(line, 5), line)

    def test_overflow_uses_the_cut_spans_style_for_the_ellipsis(self) -> None:
        line = (Span("■ ", "error", True), Span("worker", "inactive", True),
                Span(" needs memory", "warning"))
        self.assertEqual(clip(line, 7), (line[0], Span("work…", "inactive", True)))
        self.assertEqual(clip(line, 0), ())
        self.assertEqual(clip(line, 1), (Span("…", "error", True),))

    def test_wide_and_combining_edges_agree_with_plain_text_cuts(self) -> None:
        for tail, cells, expected in (("界語z", 6, "# 界…"),
                                       ("e\u0301界z", 4, "# e\u0301…"),
                                       ("界z", 3, "# …")):
            with self.subTest(tail=tail, cells=cells):
                line = (Span("# ", "default", True), Span(tail, "success", True))
                clipped = clip(line, cells)
                self.assertEqual(plain([clipped]), expected)
                self.assertEqual(plain([clipped]), cut_tail(plain([line]), cells))
                self.assertEqual(clipped[-1].tone, "success")
                self.assertTrue(clipped[-1].bold)
                self.assertLessEqual(terminal_width(plain([clipped])), cells)


class SerializationTests(unittest.TestCase):
    def test_plain_joins_spans_without_adding_color_spacing_or_a_trailing_newline(self) -> None:
        lines: list[Line] = [
            (Span("Build ", "default", True), Span("queue", "inactive")),
            (),
            (Span("●", "success"), Span(" ■", "error"), Span(" ↑27", "warning")),
        ]
        self.assertEqual(plain(lines), "Build queue\n\n● ■ ↑27")
        self.assertEqual(plain([]), "")
        self.assertEqual(plain([(), ()]), "\n")

    def test_json_form_emits_the_exact_pane_contract_and_all_styles(self) -> None:
        lines: list[Line] = [
            (Span("Build queue", "default", True),), (),
            (Span("●", "success"), Span(" ■", "error", True),
             Span(" ↑27", "warning"), Span(" 界e\u0301", "inactive")),
        ]
        result = json_form(lines, 89, 1793360772.25)
        expected = {
            "columns": 89, "as_of": 1793360772.25,
            "lines": [
                [{"text": "Build queue", "bold": True}], [],
                [{"text": "●", "tone": "success"},
                 {"text": " ■", "tone": "error", "bold": True},
                 {"text": " ↑27", "tone": "warning"},
                 {"text": " 界e\u0301", "tone": "inactive"}],
            ],
        }
        self.assertEqual(result, expected)
        self.assertEqual(cast(object, json.loads(json.dumps(result))), expected)
        self.assertEqual(plain(lines), "Build queue\n\n● ■ ↑27 界e\u0301")

    def test_a_default_span_serializes_only_its_text(self) -> None:
        self.assertEqual(json_form([(Span("plain queue text"),)], 60, 100.0)["lines"],
                         [[{"text": "plain queue text"}]])

    def test_cut_and_wrapped_spans_keep_their_background(self) -> None:
        line = (Span("ab", "success", background="suggestion"),
                Span("cd", "success", background="suggestion"))
        clipped = clip(line, 3)
        self.assertEqual(plain([clipped]), "ab…")
        self.assertEqual([span.background for span in clipped],
                         ["suggestion", "suggestion"])
        wrapped = wrap(line, 2, indent=0)
        self.assertEqual([plain([row]) for row in wrapped], ["ab", "cd"])
        wire = json_form(wrapped, 2, 100.0)
        self.assertEqual([span.get("background") for row in wire["lines"] for span in row],
                         ["suggestion", "suggestion"])
        self.assertEqual(plain(wrapped), "ab\ncd")

    def test_serialized_spans_are_detached_from_the_rendered_lines(self) -> None:
        lines = [(Span("■ 1", "error", True),)]
        first = json_form(lines, 80, 1.0)
        first["lines"][0][0]["text"] = "changed by a caller"
        first["lines"].append([])
        self.assertEqual(plain(lines), "■ 1")
        self.assertEqual(json_form(lines, 140, 2.0), {
            "columns": 140, "as_of": 2.0,
            "lines": [[{"text": "■ 1", "tone": "error", "bold": True}]],
        })


if __name__ == "__main__":
    _ = unittest.main()
