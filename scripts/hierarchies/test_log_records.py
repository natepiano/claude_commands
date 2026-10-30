"""Check phone report blocks and read their displayed fields for CLI tests."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
import unittest

from scripts.hierarchies import log_records


def phone_overflow_lines(output: str) -> list[str]:
    return [line for line in output.splitlines()
            if len(line) > log_records.PHONE_WIDTH
            and not (len(line.split()) == 1 and len(line.strip()) > log_records.PHONE_WIDTH)]


def summary_lines(output: str, prefixes: tuple[str, ...]) -> list[str]:
    lines: list[str] = []
    for line in output.split("\n\n", 1)[0].splitlines():
        if line.startswith(prefixes):
            lines.append(line)
        else:
            lines[-1] += " " + line
    return lines


def displayed_fields(lines: Sequence[str], shared: dict[str, str]) -> dict[str, str]:
    fields = dict(shared)
    current = ""
    for line in lines:
        label, separator, value = line.strip().partition(":")
        if not line.startswith("    ") and separator and label in {"value", "n", "window", "source", "Window", "Source"}:
            current = label.lower()
            fields[current] = " ".join(value.split())
        elif current:
            fields[current] = " ".join((fields[current] + " " + line.strip()).split())
        elif line.startswith("    "):
            raise ValueError(f"continuation without a field: {line!r}")
    return fields


def read_report_blocks(output: str) -> list[log_records.MeasureRow]:
    rows: list[log_records.MeasureRow] = []
    shared: dict[str, str] = {}
    for paragraph in output.strip().split("\n\n"):
        lines = paragraph.splitlines()
        if not any(line.startswith("  value:") for line in lines):
            shared = displayed_fields(lines, {})
            continue
        first_field = next(index for index, line in enumerate(lines) if line.startswith("  "))
        fields = displayed_fields(lines[first_field:], shared)
        rows.append(log_records.MeasureRow(
            " ".join(" ".join(lines[:first_field]).split()), fields["value"], int(fields["n"]),
            fields["window"], fields["source"],
        ))
    return rows


class ReportTimeTest(unittest.TestCase):
    def test_date_only_window_end_includes_that_day_and_time_end_stays_exclusive(self) -> None:
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        expected = log_records.Window(log_records.required_time("2026-10-23"), log_records.required_time("2026-10-31"))
        self.assertEqual(log_records.parse_window("2026-10-23..2026-10-30"), expected)
        self.assertEqual(log_records.window_bounds("2026-10-23", "2026-10-30", now), expected)
        timed = log_records.parse_window("2026-10-23T12:00:00Z..2026-10-30T12:00:00Z")
        self.assertEqual(timed, log_records.Window(datetime(2026, 10, 23, 12, tzinfo=timezone.utc),
                                                  datetime(2026, 10, 30, 12, tzinfo=timezone.utc)))
        self.assertFalse(timed.contains(timed.until))
        for text in ("2026-10-31..2026-10-30", "2026-10-30T12:00Z..2026-10-30T12:00Z"):
            with self.subTest(window=text), self.assertRaises(ValueError):
                _ = log_records.parse_window(text)

    def test_strict_timestamps_keep_offsets_and_refuse_an_unrecorded_offset(self) -> None:
        expected = datetime(2026, 10, 13, 21, 24, tzinfo=timezone.utc)
        for value in ("2026-10-13T21:24:00Z", "2026-10-13T17:24:00-04:00", expected):
            with self.subTest(value=value):
                at = log_records.strict_time(value)
                self.assertEqual(at, expected)
                self.assertIsNotNone(at.utcoffset())
        for value in ("2026-10-13T17:24:00", datetime(2026, 10, 13, 17, 24)):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "offset"):
                _ = log_records.strict_time(value)
        for value in ("bad time", "", 1791926640):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = log_records.strict_time(value)
        self.assertEqual(log_records.required_time("2026-10-13T17:24:00").tzinfo,
                         log_records.required_time("2026-10-13").tzinfo)


class SourceProblemTest(unittest.TestCase):
    def test_problem_identity_preserves_source_kind_and_skipped_count(self) -> None:
        state = log_records.SourceProblem("same input", "access denied", log_records.SourceNotApplicable.STATE)
        phases = log_records.SourceProblem("same input", "access denied", log_records.EventSource.PHASES)
        skipped_one = log_records.SourceProblem("same input", "access denied", log_records.EventSource.PHASES,
                                                log_records.RowsSkipped(1))
        skipped_two = log_records.SourceProblem("same input", "access denied", log_records.EventSource.PHASES,
                                                log_records.RowsSkipped(2))
        repeated = log_records.SourceProblem("same input", "access denied", log_records.EventSource.PHASES)
        problems = list(dict.fromkeys((state, phases, skipped_one, skipped_two, repeated)))
        self.assertEqual(problems, [state, phases, skipped_one, skipped_two])
        window = log_records.parse_window("2026-10-13..2026-10-19")
        rows = log_records.source_problem_rows(problems, window)
        self.assertEqual([(row.event_source, row.quantity) for row in rows], [
            (log_records.SourceNotApplicable.STATE, log_records.NotRecorded("access denied")),
            (log_records.EventSource.PHASES, log_records.NotRecorded("access denied")),
            (log_records.EventSource.PHASES, log_records.Diagnostic(1, "access denied")),
            (log_records.EventSource.PHASES, log_records.Diagnostic(2, "access denied")),
        ])


class ReportBlocksTest(unittest.TestCase):
    def test_shared_window_and_source_appear_once_above_the_row_blocks(self) -> None:
        window = "2026-10-13 17:24 EDT to 2026-10-20 17:24 EDT (end excluded)"
        source = "progress history with recorded phase identities"
        rows = [log_records.MeasureRow("Hana waits", "12.5 min", 3, window, source),
                log_records.MeasureRow("Hana phases", "4", 4, window, source)]
        output = log_records.blocks("Recorded flow", rows)
        heading, *row_blocks = output.split("\n\n")
        self.assertIn("Window:", heading)
        self.assertIn("Source:", heading)
        self.assertEqual(output.count("Window:"), 1)
        self.assertEqual(output.count("Source:"), 1)
        self.assertEqual(len(row_blocks), 2)
        self.assertTrue(all("  window:" not in block and "  source:" not in block for block in row_blocks))
        self.assertEqual(read_report_blocks(output), rows)
        self.assertEqual(phone_overflow_lines(output), [])

    def test_differing_window_and_source_stay_in_each_row(self) -> None:
        rows = [log_records.MeasureRow("First row", "not recorded", 0, "first window", "first source"),
                log_records.MeasureRow("Second row", "2.0 min", 2, "second window", "second source")]
        output = log_records.blocks("Different evidence", rows)
        self.assertEqual(output.count("  window:"), 2)
        self.assertEqual(output.count("  source:"), 2)
        self.assertNotIn("\nWindow:", output)
        self.assertNotIn("\nSource:", output)
        self.assertEqual(read_report_blocks(output), rows)

    def test_only_the_shared_field_is_removed_from_blocks(self) -> None:
        for shared_field in ("window", "source"):
            with self.subTest(shared_field=shared_field):
                rows = [log_records.MeasureRow("First", "one", 1, "same window", "same source"),
                        log_records.MeasureRow("Second", "two", 2,
                                              "same window" if shared_field == "window" else "new window",
                                              "same source" if shared_field == "source" else "new source")]
                output = log_records.blocks("Partly shared", rows)
                differing = "source" if shared_field == "window" else "window"
                self.assertEqual(output.count(shared_field.capitalize() + ":"), 1)
                self.assertNotIn("  " + shared_field + ":", output)
                self.assertEqual(output.count("  " + differing + ":"), 2)
                self.assertEqual(read_report_blocks(output), rows)

    def test_equal_values_and_counts_remain_in_each_row_block(self) -> None:
        rows = [log_records.MeasureRow("First phase", "0.0 min", 0, "window", "source"),
                log_records.MeasureRow("Second phase", "0.0 min", 0, "window", "source")]
        output = log_records.blocks("Recorded zero waits", rows)
        self.assertEqual(output.count("  value: 0.0 min"), 2)
        self.assertEqual(output.count("  n: 0"), 2)
        self.assertEqual(read_report_blocks(output), rows)

    def test_long_titles_names_and_all_fields_wrap_without_losing_words(self) -> None:
        title = "Flow to Hana main with recorded phase issue completion and local reference merge commits"
        rows = [log_records.MeasureRow(
            "Hana wait coverage with several possible recorded phase candidates and an unresolved holder",
            "completed duration is not recorded because the phase has several possible candidates (6 times)",
            6, "the first recorded window with an excluded end and its full time zone label",
            "the first source names all candidate phases and retains each reason for missing evidence",
        ), log_records.MeasureRow("Other phase", "5.0 min (partial)", 1, "different window", "different source")]
        output = log_records.blocks(title, rows)
        self.assertEqual(" ".join(output.split("\n\n", 1)[0].split()), title)
        self.assertEqual(read_report_blocks(output), rows)
        self.assertEqual(phone_overflow_lines(output), [])
        continuations = [line for line in output.splitlines() if line.startswith("    ")]
        self.assertTrue(continuations)
        self.assertTrue(all(not line.startswith("     ") for line in continuations))

    def test_a_long_path_or_hash_stands_alone_and_is_never_split(self) -> None:
        path = "/archive/" + "long-path-segment-" * 6
        commit = "c" * 80
        rows = [log_records.MeasureRow("Merge evidence", "before " + commit + " after", 1,
                                      "window", "read " + path + " without changing it")]
        output = log_records.blocks("Receipt evidence", rows)
        self.assertEqual([line.strip() for line in output.splitlines() if len(line) > log_records.PHONE_WIDTH],
                         [path, commit])
        self.assertEqual(read_report_blocks(output), rows)
        self.assertEqual(phone_overflow_lines(output), [])

    def test_continuation_indent_never_makes_a_fitting_word_exceed_the_phone_width(self) -> None:
        for word_length in range(log_records.PHONE_WIDTH - 4, log_records.PHONE_WIDTH + 1):
            for shared in (False, True):
                with self.subTest(word_length=word_length, shared=shared):
                    word = "v" * word_length
                    evidence = "before " + word + " after"
                    rows = [log_records.MeasureRow(evidence, evidence, int("7" * word_length), evidence, evidence)]
                    if not shared:
                        rows.append(log_records.MeasureRow("Other wait", "2.0 min", 2, "other window", "other source"))
                    output = log_records.blocks("Recorded " + word + " duration", rows)
                    self.assertEqual(phone_overflow_lines(output), [])
                    self.assertEqual(read_report_blocks(output), rows)

    def test_empty_rows_print_a_wrapped_title_and_none(self) -> None:
        title = "Recorded Hana phase wait evidence without any rows in the requested report window"
        output = log_records.blocks(title, [])
        self.assertEqual(output.splitlines()[-1], "none")
        self.assertEqual(" ".join(output.splitlines()[:-1]), title)
        self.assertEqual(phone_overflow_lines(output), [])


if __name__ == "__main__":
    _ = unittest.main()
