#!/usr/bin/env python3
"""Tests for candidates.py: the nightly review's open candidates and the ledger lines a report adds."""
from __future__ import annotations

import contextlib
import io
import os
import socket
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast, final, override
from unittest import mock
from zoneinfo import ZoneInfo

import candidates
from candidates import Candidate, TimeGain, TokenGain, Unmeasured

# Importable once candidates has put scripts/lib on the path.
import urgency


def setUpModule() -> None:
    """The times these tests expect are Eastern; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}))


LEDGER: str = """\
- 2026-09-28 config: Put cargo on the job PATH — accepted, applied; from Also found: merge two hooks — accepted 2026-10-01
- 2026-10-01 config: Say the handoff instruction once — proposed
- 2026-10-05 rust/nateroids: Build CI tests without debug info [gain/week: 2.5 h red CI] — deferred
- 2026-10-11 config, also found: Duplicate CLAUDE.md in worktrees [gain/week: 0.4% of tokens] — proposed
- 2026-10-12 config: Turn off the Artifact tool: 10.5k tokens, never called [gain/week: 3.1% of tokens] — proposed
- 2026-10-12 rust/hana: Run the GPU tests 8 at a time [gain/week: 38 min waiting] — proposed
- 2026-10-12 rust/hana, noted: The reload tests share one port — declined
- 2026-09-29-evening config: Allow read-only Bash [gain/week: about a third of calls] — proposed
"""

REPORT: str = """\
# Run the GPU tests 8 at a time
**Target:** rust/hana
**Innovation:** Raise the test thread count.
**Measurable improvement:** suite time: 412 s → 180 s, measured by `cargo nextest run`
**Gain per week:** 38 min waiting.
**Evidence:** 14 days of runs.
**Cost:** one config line.
**Also found:**
- **The reload tests share one port.** Two of them fail when run together [gain/week: 12 min red CI]
- **Stale style rule.** `use-kana.md` names a crate that is gone.
  Its detail runs on, indented.
3. A plain item: it has no bold lead.

Denied tonight:
- A `sed` read was blocked by a deny rule.
**Noted:**
- **Target choice was close.** hana scored 0.60.
"""


@final
class CandidatesTests(unittest.TestCase):
    root: Path = Path()
    note: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.note = self.root / "nightly improvement list.md"
        with contextlib.ExitStack() as environment:
            _ = cast(object, environment.enter_context(mock.patch.dict(
                os.environ, {"NIGHTLY_REVIEW_NOTE": str(self.note), "NEXT_STATE_DIR": str(self.root / "next")})))
            self.addCleanup(environment.pop_all().close)
        patch = mock.patch.object(candidates, "ROOT", self.root)
        _ = patch.start()
        self.addCleanup(patch.stop)

    def run_main(self, arguments: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = candidates.main(arguments)
        return code, out.getvalue(), err.getvalue()

    def test_record_repairs_an_interrupted_last_heading_before_queueing_it(self) -> None:
        report = self.root / "2026-10-12/report.md"
        report.parent.mkdir()
        _ = report.write_text(REPORT)
        ledger = self.root / "ledger.md"
        expected = self.run_main(["record", str(report)])
        self.assertEqual(expected[0], 0, expected[2])
        expected_ledger = ledger.read_bytes()
        first = expected[1].splitlines()[0]
        for tail in (first.split(" — ")[0], first.removesuffix("posed")):
            with self.subTest(tail=tail):
                _ = ledger.write_text(tail)
                with mock.patch("scripts.lib.atomic_file.os.replace", side_effect=OSError("publish stopped")):
                    stopped = self.run_main(["record", str(report)])
                self.assertEqual(stopped[0], 1)
                retried = self.run_main(["record", str(report)])
                self.assertEqual(retried, expected)
                self.assertEqual(ledger.read_bytes(), expected_ledger)
                self.assertEqual(len(candidates.ordered(ledger.read_text().splitlines())), 5)

    def test_a_gain_reads_as_time_tokens_or_unmeasured(self) -> None:
        self.assertEqual(candidates.read_gain("38 min waiting"), TimeGain(38.0))
        self.assertEqual(candidates.read_gain("2.5 h red CI"), TimeGain(150.0))
        self.assertEqual(candidates.read_gain("1,200 minutes of failed runs"), TimeGain(1200.0))
        self.assertEqual(candidates.read_gain("3.1% of tokens"), TokenGain(3.1))
        for text in ("", "about a third of calls", "40 hits a day"):
            self.assertEqual(candidates.read_gain(text), Unmeasured())

    def test_record_keeps_every_outcome_written_by_next(self) -> None:
        report = self.root / "2026-10-12/report.md"
        report.parent.mkdir()
        _ = report.write_text("# Finished candidate\n**Target:** config\n**Gain per week:** 12 min waiting\n")
        ledger = self.root / "ledger.md"
        for outcome in ("proposed", "deferred", "accepted 2026-10-13, applied (config abc123)",
                        "declined", "done 2026-10-13", "relayed 2026-10-13"):
            with self.subTest(outcome=outcome):
                decided = f"- 2026-10-12 config: Finished candidate [gain/week: 12 min waiting] — {outcome}\n"
                _ = ledger.write_text(decided)
                self.assertEqual(self.run_main(["record", str(report)]), (0, "nothing new to record\n", ""))
                self.assertEqual(ledger.read_text(), decided)
                if outcome.startswith(("done ", "relayed ")):
                    self.assertEqual(self.run_main([]), (0, "no open candidates\n", ""))

    def test_record_finishes_partial_records_with_outcome_words_in_the_title(self) -> None:
        report = self.root / "2026-10-12/report.md"
        report.parent.mkdir()
        ledger = self.root / "ledger.md"
        for title in ("Mark — done before publishing", "Notice — relayed to another session"):
            for gain in ("", "**Gain per week:** 12 min waiting\n"):
                with self.subTest(title=title, gain=gain):
                    _ = report.write_text(f"# {title}\n**Target:** config\n{gain}")
                    ledger.unlink(missing_ok=True)
                    expected = self.run_main(["record", str(report)])
                    self.assertEqual(expected[0], 0, expected[2])
                    expected_ledger = ledger.read_bytes()
                    for tail in (expected[1].rsplit(" — ", 1)[0], expected[1].rstrip().removesuffix("posed")):
                        with self.subTest(tail=tail):
                            _ = ledger.write_text(tail)
                            self.assertEqual(self.run_main(["record", str(report)]), expected)
                            self.assertEqual(ledger.read_bytes(), expected_ledger)
                            self.assertEqual([item.title for item in candidates.ordered(ledger.read_text().splitlines())], [title])

    def test_record_retries_output_when_printing_stops_before_publication(self) -> None:
        report = self.root / "2026-10-12/report.md"
        report.parent.mkdir()
        _ = report.write_text(REPORT)
        ledger = self.root / "ledger.md"
        expected = self.run_main(["record", str(report)])
        self.assertEqual(expected[0], 0, expected[2])
        expected_ledger = ledger.read_bytes()
        ledger.unlink()

        with mock.patch("builtins.print", side_effect=RuntimeError("output stopped")):
            with self.assertRaisesRegex(RuntimeError, "output stopped"):
                _ = self.run_main(["record", str(report)])

        self.assertFalse(ledger.exists())
        self.assertEqual(self.run_main(["record", str(report)]), expected)
        self.assertEqual(ledger.read_bytes(), expected_ledger)

    def test_only_lines_that_end_proposed_or_deferred_are_open(self) -> None:
        found = candidates.ranked(LEDGER.splitlines())
        self.assertEqual(len(found), 6)
        self.assertNotIn("Put cargo on the job PATH", [each.title for each in found])
        self.assertNotIn("The reload tests share one port", [each.title for each in found])

    def test_time_ranks_before_tokens_and_unmeasured_comes_last_oldest_night_first(self) -> None:
        self.assertEqual([each.title for each in candidates.ranked(LEDGER.splitlines())], [
            "Build CI tests without debug info",
            "Run the GPU tests 8 at a time",
            "Turn off the Artifact tool: 10.5k tokens, never called",
            "Duplicate CLAUDE.md in worktrees",
            "Allow read-only Bash",
            "Say the handoff instruction once",
        ])

    def test_next_shows_one_candidate_until_its_outcome_and_a_moved_one_goes_last(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        first = "- 2026-10-05 rust/nateroids: Build CI tests without debug info [gain/week: 2.5 h red CI] — deferred"
        self.assertEqual(self.run_main(["next"]), (0, first + "\n", ""))
        urgency.move_to_end(candidates.QUEUE, "- 2026-10-05 rust/nateroids: Build CI tests without debug info",
                                       datetime(2026, 10, 22, 10, 0))
        second = "- 2026-10-12 rust/hana: Run the GPU tests 8 at a time [gain/week: 38 min waiting] — proposed"
        self.assertEqual(self.run_main(["next"]), (0, second + "\n", ""))
        titles = [each.title for each in candidates.ordered(LEDGER.splitlines())]
        self.assertEqual((titles[0], titles[-1]), ("Run the GPU tests 8 at a time", "Build CI tests without debug info"))
        code, out, _ = self.run_main([])
        self.assertEqual((code, out.splitlines()[2].split(" | ")[0]), (0, "| recommended"))

    def test_a_line_gives_its_night_area_gain_and_state(self) -> None:
        self.assertEqual(
            candidates.open_candidate("- 2026-10-11 config, also found: Duplicate CLAUDE.md [gain/week: 0.4% of tokens] — proposed"),
            Candidate("2026-10-11", "config", "also found", "Duplicate CLAUDE.md", "0.4% of tokens", "proposed"),
        )
        evening = candidates.open_candidate("- 2026-09-29-evening rust/hana: One feature set — deferred")
        self.assertEqual(evening, Candidate("2026-09-29-evening", "rust/hana", "", "One feature set", "", "deferred"))
        self.assertIsNone(candidates.open_candidate("One line per night."))

    def test_the_table_marks_the_first_measured_row_recommended(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        code, out, err = self.run_main([])
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "| Pick | Night | Area | Candidate | Gain per week | State |")
        self.assertEqual(lines[2], "| recommended | 2026-10-05 | rust/nateroids | Build CI tests without debug info | 2.5 h red CI | deferred |")
        self.assertEqual(lines[5], "|  | 2026-10-11 | config, also found | Duplicate CLAUDE.md in worktrees | 0.4% of tokens | proposed |")
        self.assertEqual(sum("recommended" in line for line in lines), 1)

    def test_no_row_is_marked_when_nothing_is_measured(self) -> None:
        _ = (self.root / "ledger.md").write_text("- 2026-10-01 config: Say it once — proposed\n", encoding="utf-8")
        _, out, _ = self.run_main([])
        self.assertEqual(out.splitlines()[2], "|  | 2026-10-01 | config | Say it once |  | proposed |")

    def test_no_ledger_or_nothing_open_says_so(self) -> None:
        self.assertEqual(self.run_main([]), (0, "no open candidates\n", ""))

    def test_printing_writes_the_table_with_read_time_and_host(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        moment = datetime(2026, 10, 12, 19, 30, 45, tzinfo=ZoneInfo("America/New_York"))
        with (
            mock.patch.object(candidates, "now", return_value=moment),
            mock.patch.object(socket, "gethostname", return_value="natedev.example.org"),
        ):
            code, out, err = self.run_main([])

        self.assertEqual((code, err), (0, ""))
        read_line = (
            "Read 2026-10-12 19:30 EDT from the nightly review's ledger on natedev. "
            "The nightly review rewrites this note; edits made here are lost."
        )
        text = self.note.read_text(encoding="utf-8")
        self.assertEqual(text, f"{read_line}\n\n{out}")
        self.assertEqual(text.split("\n\n", 1)[1], out)
        self.assertEqual(self.note.stat().st_mode & 0o777, 0o644)
        self.assertEqual(list(self.root.glob(".*")), [])

    def test_january_read_time_uses_est(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        moment = datetime(2026, 1, 12, 19, 30, tzinfo=ZoneInfo("America/New_York"))
        with (
            mock.patch.object(candidates, "now", return_value=moment),
            mock.patch.object(socket, "gethostname", return_value="macbook"),
        ):
            code, out, err = self.run_main([])

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            self.note.read_text(encoding="utf-8"),
            "Read 2026-01-12 19:30 EST from the nightly review's ledger on macbook. "
            + "The nightly review rewrites this note; edits made here are lost.\n\n" + out,
        )

    def test_ledger_with_no_open_candidates_writes_the_empty_list(self) -> None:
        _ = (self.root / "ledger.md").write_text(
            "- 2026-10-12 config: Earlier — accepted 2026-10-13\n", encoding="utf-8"
        )
        moment = datetime(2026, 10, 12, 19, 30, tzinfo=ZoneInfo("America/New_York"))
        with (
            mock.patch.object(candidates, "now", return_value=moment),
            mock.patch.object(socket, "gethostname", return_value="natedev"),
        ):
            self.assertEqual(self.run_main([]), (0, "no open candidates\n", ""))

        self.assertEqual(
            self.note.read_text(encoding="utf-8"),
            "Read 2026-10-12 19:30 EDT from the nightly review's ledger on natedev. "
            + "The nightly review rewrites this note; edits made here are lost.\n\n"
            + "no open candidates\n",
        )

    def test_record_writes_current_ledger_and_rewrites_when_nothing_is_new(self) -> None:
        night = self.root / "2026-10-12"
        night.mkdir()
        report = night / "rust.md"
        _ = report.write_text(REPORT, encoding="utf-8")
        ledger = self.root / "ledger.md"
        _ = ledger.write_text("- 2026-10-11 config: Earlier — proposed\n", encoding="utf-8")
        moment = datetime(2026, 10, 12, 19, 30, tzinfo=ZoneInfo("America/New_York"))
        with (
            mock.patch.object(candidates, "now", return_value=moment),
            mock.patch.object(socket, "gethostname", return_value="natedev"),
        ):
            code, out, err = self.run_main(["record", str(report)])
            self.assertEqual((code, err), (0, ""))
            self.assertIn("8 at a time", out)
            body = candidates.table(candidates.ranked(ledger.read_text(encoding="utf-8").splitlines()))
            expected = (
                "Read 2026-10-12 19:30 EDT from the nightly review's ledger on natedev. "
                + "The nightly review rewrites this note; edits made here are lost.\n\n"
                + body + "\n"
            )
            self.assertEqual(self.note.read_text(encoding="utf-8"), expected)
            _ = self.note.write_text("old copy\n", encoding="utf-8")
            self.assertEqual(self.run_main(["record", str(report)]), (0, "nothing new to record\n", ""))
            self.assertEqual(self.note.read_text(encoding="utf-8"), expected)

    def test_record_with_no_new_or_open_candidates_writes_the_empty_list(self) -> None:
        night = self.root / "2026-10-12"
        night.mkdir()
        report = night / "config.md"
        _ = report.write_text("# Earlier\n**Target:** config\n", encoding="utf-8")
        _ = (self.root / "ledger.md").write_text(
            "- 2026-10-12 config: Earlier — accepted 2026-10-13\n", encoding="utf-8"
        )

        self.assertEqual(self.run_main(["record", str(report)]), (0, "nothing new to record\n", ""))
        self.assertEqual(self.note.read_text(encoding="utf-8").split("\n\n", 1)[1], "no open candidates\n")

    def test_missing_note_folder_is_not_created(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        note = self.root / "missing" / "nightly.md"
        with mock.patch.dict(os.environ, {"NIGHTLY_REVIEW_NOTE": str(note)}):
            code, out, err = self.run_main([])

        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("| Pick |"))
        self.assertFalse(note.parent.exists())

    def test_missing_ledger_keeps_an_existing_note(self) -> None:
        _ = self.note.write_text("table from another machine\n", encoding="utf-8")

        self.assertEqual(self.run_main([]), (0, "no open candidates\n", ""))
        self.assertEqual(self.note.read_text(encoding="utf-8"), "table from another machine\n")

    def test_note_path_reads_current_environment_and_expands_home(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        alternate = self.root / "alternate.md"
        with (
            mock.patch.dict(os.environ, {"NIGHTLY_REVIEW_NOTE": "~/alternate.md"}),
            mock.patch.object(Path, "expanduser", autospec=True, return_value=alternate) as expand_home,
        ):
            code, out, err = self.run_main([])

        self.assertEqual((code, err), (0, ""))
        expand_home.assert_called_once_with(Path("~/alternate.md"))
        self.assertEqual(alternate.read_text(encoding="utf-8").split("\n\n", 1)[1], out)
        self.assertFalse(self.note.exists())

    def test_empty_or_unset_note_path_uses_the_default_under_a_temporary_home(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        default = self.root / "rust" / "hanadocs" / "nightly improvement list.md"
        default.parent.mkdir(parents=True)
        for value in ("", None):
            with self.subTest(value=value), mock.patch.dict(os.environ), mock.patch.object(Path, "home", return_value=self.root):
                if value is None:
                    del os.environ["NIGHTLY_REVIEW_NOTE"]
                else:
                    os.environ["NIGHTLY_REVIEW_NOTE"] = value
                code, out, err = self.run_main([])
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(default.read_text(encoding="utf-8").split("\n\n", 1)[1], out)
                default.unlink()
        self.assertFalse(self.note.exists())

    def test_record_creates_ledger_and_note_when_neither_exists(self) -> None:
        night = self.root / "2026-10-12"
        night.mkdir()
        report = night / "config.md"
        _ = report.write_text("# New item\n**Target:** config\n", encoding="utf-8")

        self.assertEqual(
            self.run_main(["record", str(report)]),
            (0, "- 2026-10-12 config: New item — proposed\n", ""),
        )
        body = self.note.read_text(encoding="utf-8").split("\n\n", 1)[1]
        self.assertEqual(
            body,
            "| Pick | Night | Area | Candidate | Gain per week | State |\n"
            + "| --- | --- | --- | --- | --- | --- |\n"
            + "|  | 2026-10-12 | config | New item |  | proposed |\n",
        )

    def test_record_skips_missing_note_folder_after_updating_ledger(self) -> None:
        night = self.root / "2026-10-12"
        night.mkdir()
        report = night / "config.md"
        _ = report.write_text("# New item\n**Target:** config\n", encoding="utf-8")
        note = self.root / "missing" / "nightly.md"
        with mock.patch.dict(os.environ, {"NIGHTLY_REVIEW_NOTE": str(note)}):
            code, out, err = self.run_main(["record", str(report)])

        self.assertEqual((code, err), (0, ""))
        self.assertEqual((self.root / "ledger.md").read_text(encoding="utf-8"), out)
        self.assertFalse(note.parent.exists())

    def test_folder_check_failure_keeps_printed_table_and_success(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        error = PermissionError("folder unavailable")
        with mock.patch.object(Path, "is_dir", autospec=True, side_effect=error) as folder_check:
            code, out, err = self.run_main([])

        folder_check.assert_called_once_with(self.note.parent)
        self.assertEqual((code, out), (0, candidates.table(candidates.ranked(LEDGER.splitlines())) + "\n"))
        self.assertEqual(err, f"candidates: cannot write {self.note}: {error}\n")
        self.assertFalse(self.note.exists())
        self.assertEqual(list(self.root.glob(".*")), [])

    def test_ledger_check_failure_keeps_printed_table_and_success(self) -> None:
        ledger = self.root / "ledger.md"
        _ = ledger.write_text(LEDGER, encoding="utf-8")
        error = OSError("ledger check unavailable")
        with mock.patch.object(Path, "is_file", autospec=True, side_effect=error) as ledger_check:
            code, out, err = self.run_main([])

        ledger_check.assert_called_once_with(ledger)
        self.assertEqual((code, out), (0, candidates.table(candidates.ranked(LEDGER.splitlines())) + "\n"))
        self.assertEqual(err, f"candidates: cannot write {self.note}: {error}\n")
        self.assertFalse(self.note.exists())
        self.assertEqual(list(self.root.glob(".*")), [])

    def test_hostname_failure_keeps_printed_table_and_success(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        error = OSError("hostname unavailable")
        with mock.patch.object(socket, "gethostname", side_effect=error) as hostname:
            code, out, err = self.run_main([])

        hostname.assert_called_once_with()
        self.assertEqual((code, out), (0, candidates.table(candidates.ranked(LEDGER.splitlines())) + "\n"))
        self.assertEqual(err, f"candidates: cannot write {self.note}: {error}\n")
        self.assertFalse(self.note.exists())
        self.assertEqual(list(self.root.glob(".*")), [])

    def test_hostname_failure_keeps_record_lines_and_success(self) -> None:
        night = self.root / "2026-10-12"
        night.mkdir()
        report = night / "config.md"
        _ = report.write_text("# New item\n**Target:** config\n", encoding="utf-8")
        line = "- 2026-10-12 config: New item — proposed\n"
        error = OSError("hostname unavailable")
        for expected in (line, "nothing new to record\n"):
            with self.subTest(expected=expected):
                with mock.patch.object(socket, "gethostname", side_effect=error) as hostname:
                    code, out, err = self.run_main(["record", str(report)])

                hostname.assert_called_once_with()
                self.assertEqual((code, out), (0, expected))
                self.assertEqual(err, f"candidates: cannot write {self.note}: {error}\n")
                self.assertEqual((self.root / "ledger.md").read_text(encoding="utf-8"), line)
                self.assertFalse(self.note.exists())
                self.assertEqual(list(self.root.glob(".*")), [])

    def test_failed_note_write_keeps_success_and_removes_temporary_file(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        self.note.mkdir()

        code, out, err = self.run_main([])

        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("| Pick |"))
        self.assertTrue(err.startswith(f"candidates: cannot write {self.note}: "))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertTrue(self.note.is_dir())
        self.assertEqual(list(self.root.glob(".*")), [])

    def test_failed_note_write_keeps_record_success_with_and_without_new_lines(self) -> None:
        night = self.root / "2026-10-12"
        night.mkdir()
        report = night / "config.md"
        _ = report.write_text("# New item\n**Target:** config\n", encoding="utf-8")
        self.note.mkdir()
        for expected in ("- 2026-10-12 config: New item — proposed\n", "nothing new to record\n"):
            with self.subTest(expected=expected):
                code, out, err = self.run_main(["record", str(report)])
                self.assertEqual((code, out), (0, expected))
                self.assertTrue(err.startswith(f"candidates: cannot write {self.note}: "))
                self.assertEqual(len(err.splitlines()), 1)
                self.assertEqual(list(self.root.glob(".*")), [])
        self.assertEqual(
            (self.root / "ledger.md").read_text(encoding="utf-8"),
            "- 2026-10-12 config: New item — proposed\n",
        )

    def test_refused_report_and_usage_error_keep_an_existing_note(self) -> None:
        _ = (self.root / "ledger.md").write_text(LEDGER, encoding="utf-8")
        _ = self.note.write_text("previous copy\n", encoding="utf-8")
        report = self.root / "config.md"
        _ = report.write_text("No report title or target.\n", encoding="utf-8")
        for arguments, expected_code in ((["record", str(report)], 1), (["rank"], 2)):
            with self.subTest(arguments=arguments):
                code, out, err = self.run_main(arguments)
                self.assertEqual((code, out), (expected_code, ""))
                self.assertTrue(err)
                self.assertEqual(self.note.read_text(encoding="utf-8"), "previous copy\n")

    def test_record_adds_the_proposal_and_each_smaller_item_once(self) -> None:
        night = self.root / "2026-10-12"
        night.mkdir()
        report = night / "rust.md"
        _ = report.write_text(REPORT, encoding="utf-8")
        _ = (self.root / "ledger.md").write_text("- 2026-10-11 config: Earlier — proposed\n", encoding="utf-8")
        expected = [
            "- 2026-10-12 rust/hana: Run the GPU tests 8 at a time [gain/week: 38 min waiting] — proposed",
            "- 2026-10-12 rust/hana, also found: The reload tests share one port [gain/week: 12 min red CI] — proposed",
            "- 2026-10-12 rust/hana, also found: Stale style rule — proposed",
            "- 2026-10-12 rust/hana, also found: A plain item — proposed",
            "- 2026-10-12 rust/hana, noted: Target choice was close — proposed",
        ]
        code, out, err = self.run_main(["record", str(report)])
        self.assertEqual((code, err, out.splitlines()), (0, "", expected))
        ledger = (self.root / "ledger.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(ledger, ["- 2026-10-11 config: Earlier — proposed", *expected])
        self.assertEqual([each.title for each in candidates.ranked(ledger)][:2],
                         ["Run the GPU tests 8 at a time", "The reload tests share one port"])
        self.assertEqual(self.run_main(["record", str(report)]), (0, "nothing new to record\n", ""))

    def test_record_keeps_a_line_whose_outcome_was_already_written(self) -> None:
        night = self.root / "2026-10-12"
        night.mkdir()
        _ = (night / "rust.md").write_text(REPORT, encoding="utf-8")
        decided = "- 2026-10-12 rust/hana: Run the GPU tests 8 at a time [gain/week: 38 min waiting] — accepted 2026-10-13\n"
        _ = (self.root / "ledger.md").write_text(decided, encoding="utf-8")
        _, out, _ = self.run_main(["record", str(night / "rust.md")])
        self.assertEqual(len(out.splitlines()), 4)
        self.assertNotIn("8 at a time", out)

    def test_a_list_below_a_paragraph_is_no_candidate(self) -> None:
        found = candidates.report_candidates("2026-10-12", REPORT.splitlines())
        self.assertNotIn("A `sed` read was blocked by a deny rule", [each.title for each in found])
        self.assertEqual([each.kind for each in found], ["", "also found", "also found", "also found", "noted"])

    def test_an_unmeasured_report_gets_no_tag(self) -> None:
        lines = ["# Tidy the docs", "**Target:** config", "**Gain per week:** unmeasured: no counter exists."]
        self.assertEqual([each.line for each in candidates.report_candidates("2026-10-12", lines)],
                         ["- 2026-10-12 config: Tidy the docs — proposed"])

    def test_record_refuses_a_report_with_no_title_or_target(self) -> None:
        report = self.root / "config.md"
        _ = report.write_text("The mode could not finish: the Mac did not answer.\n", encoding="utf-8")
        code, out, err = self.run_main(["record", str(report)])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("to record", err)
        self.assertFalse((self.root / "ledger.md").exists())
        self.assertFalse(self.note.exists())

    def test_main_rejects_other_arguments(self) -> None:
        code, _, err = self.run_main(["rank", "everything"])
        self.assertEqual(code, 2)
        self.assertIn("usage: candidates.py", err)
        self.assertFalse(self.note.exists())


if __name__ == "__main__":
    _ = unittest.main()
