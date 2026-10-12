#!/usr/bin/env python3
"""Tests for candidates.py: the nightly review's open candidates and the ledger lines a report adds."""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest import mock

import candidates
from candidates import Candidate, TimeGain, TokenGain, Unmeasured

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

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        patch = mock.patch.object(candidates, "ROOT", self.root)
        _ = patch.start()
        self.addCleanup(patch.stop)

    def run_main(self, arguments: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = candidates.main(arguments)
        return code, out.getvalue(), err.getvalue()

    def test_a_gain_reads_as_time_tokens_or_unmeasured(self) -> None:
        self.assertEqual(candidates.read_gain("38 min waiting"), TimeGain(38.0))
        self.assertEqual(candidates.read_gain("2.5 h red CI"), TimeGain(150.0))
        self.assertEqual(candidates.read_gain("1,200 minutes of failed runs"), TimeGain(1200.0))
        self.assertEqual(candidates.read_gain("3.1% of tokens"), TokenGain(3.1))
        for text in ("", "about a third of calls", "40 hits a day"):
            self.assertEqual(candidates.read_gain(text), Unmeasured())

    def test_only_lines_that_end_proposed_or_deferred_are_open(self) -> None:
        found = candidates.ranked(LEDGER.splitlines())
        self.assertEqual(len(found), 6)
        self.assertNotIn("Put cargo on the job PATH", [each.title for each in found])
        self.assertNotIn("The reload tests share one port", [each.title for each in found])

    def test_time_ranks_before_tokens_and_unmeasured_comes_last_in_ledger_order(self) -> None:
        self.assertEqual([each.title for each in candidates.ranked(LEDGER.splitlines())], [
            "Build CI tests without debug info",
            "Run the GPU tests 8 at a time",
            "Turn off the Artifact tool: 10.5k tokens, never called",
            "Duplicate CLAUDE.md in worktrees",
            "Say the handoff instruction once",
            "Allow read-only Bash",
        ])

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

    def test_main_rejects_other_arguments(self) -> None:
        code, _, err = self.run_main(["rank", "everything"])
        self.assertEqual(code, 2)
        self.assertIn("usage: candidates.py", err)


if __name__ == "__main__":
    _ = unittest.main()
