#!/usr/bin/env python3
"""Tests for proposals_table.py: the showrunner's table of proposals still open for the user."""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from collections.abc import Generator
from pathlib import Path
from typing import final, override
from unittest import mock

import proposals_table
import showrunners
from proposals_table import ADD_ON_STATE, REVIEW_STATE, Row, Worktree

# Importable once proposals_table has put scripts/delegate on the path.
import phase_table
import progress_history


@final
class ProposalsTableTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    @contextlib.contextmanager
    def runs_of(self, runs: dict[Path, list[Path]]) -> Generator[None]:
        """Give each plan path the run files named for it, and every other path none."""
        def plan_runs(plan: Path) -> list[Path]:
            return runs.get(plan, [])
        with mock.patch.object(progress_history, "plan_runs", side_effect=plan_runs):
            yield

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")
        return path

    def test_the_backlog_gives_its_proposed_and_deferred_lines_only(self) -> None:
        _ = self.write("backlog/ledger.md", "\n".join([
            "# Plan backlog",
            "",
            "One line per proposed plan: `- <date> <title> (<file>) — proposed|accepted <date>|declined|deferred`.",
            "",
            "- 2026-10-09 Nested showrunners (2026-10-09-nested.md) — accepted 2026-10-12, build later",
            "- 2026-10-11 Codex dots: recommend wait (2026-10-11-dots.md) — proposed",
            "- 2026-10-10 Config overview ([[2026-10-10-overview]]) — deferred",
            "- 2026-10-08 A plan with no file — proposed",
            "- 2026-10-07 Dropped idea (2026-10-07-dropped.md) — declined",
        ]))
        _ = self.write("backlog/2026-10-11-dots.md", "# Dots\n\nProposed 2026-10-11 by a plan subagent from natedev, at the user's request.\n")
        _ = self.write("backlog/2026-10-10-overview.md", "# Overview\n\nNo proposer is named here.\n")
        self.assertEqual(proposals_table.backlog_rows(self.root / "backlog"), [
            Row("a plan subagent from natedev", "Codex dots: recommend wait", "2026-10-11", REVIEW_STATE),
            Row("plan backlog", "Config overview", "2026-10-10", "deferred until you ask for it"),
            Row("plan backlog", "A plan with no file", "2026-10-08", REVIEW_STATE),
        ])

    def test_a_missing_backlog_gives_no_rows(self) -> None:
        self.assertEqual(proposals_table.backlog_rows(self.root / "absent"), [])

    def test_a_proposal_doc_shows_while_its_status_says_proposal(self) -> None:
        _ = self.write("unit/docs/plans/proposal-review-unit.md", "\n".join([
            "# Proposal: a review unit with reviewer pools",
            "",
            "> **Status: PROPOSAL — for the user's decision.** A production starts one review",
            "> unit. Written 2026-10-10 by a model-study research agent.",
        ]))
        _ = self.write("unit/docs/plans/proposal-decided.md", "# Proposal: decided\n\n> **Status: ACCEPTED 2026-10-11.** Built.\n")
        _ = self.write("unit/docs/plans/other-plan.md", "# Not a proposal\n")
        self.assertEqual(proposals_table.doc_rows([Worktree("model-study-unit", self.root / "unit")]), [
            Row("a model-study research agent", "a review unit with reviewer pools", "2026-10-10", REVIEW_STATE),
        ])

    def test_a_doc_merged_into_the_trunk_is_one_row_under_its_unit(self) -> None:
        text = "# Proposal: faster footers\n\nNo status and no author line.\n"
        unit = self.write("unit/docs/plans/proposal-footers.md", text)
        trunk = self.write("trunk/docs/plans/proposal-footers.md", text)
        for path in (unit, trunk):
            os.utime(path, (1_760_000_000, 1_760_000_000))
        rows = proposals_table.doc_rows([Worktree("trunk", self.root / "trunk"), Worktree("footer-unit", self.root / "unit")])
        self.assertEqual(rows, [Row("footer-unit", "faster footers", proposals_table.file_date(unit), REVIEW_STATE)])

    def test_the_index_gives_every_row_whose_status_is_not_closed(self) -> None:
        index = self.write("proposals/index.md", "\n".join([
            "# Proposals",
            "",
            "Finished proposals waiting for your review.",
            "",
            "| Date | Proposal | Unit | Summary | Status |",
            "| --- | --- | --- | --- | --- |",
            "| 2026-10-09 | [Selection widgets toggle](selection-widgets-toggle.md) | trunk | One toggle hides the idle widgets; every operation in two steps | waiting for review |",
            "| 2026-10-09 | [Camera jack panel trial](camera-jack-panel-trial.md) | organon | Camera jacks in outlined groups | approved 2026-10-10: build after the title bar |",
            "| 2026-10-09 | [Shortcuts](shortcuts.md) | demo | Every shortcut by category; 8 inspectors become commands | in review |",
            "| 2026-10-10 | [Rich text](rich-text.md) | research | One text box with styled runs | Done 2026-10-11 (widget) |",
            "| 2026-10-10 | [Old pie](old-pie.md) | organon | A pie of 12 | superseded by selection-action-pie.md |",
            "| 2026-10-11 | Plain title, a \\| in it | | | |",
            "",
            "| Unrelated | Table |",
            "| --- | --- |",
            "| 2026-10-12 | not a proposal |",
        ]))
        self.assertEqual(proposals_table.index_rows(index), [
            Row("trunk", "Selection widgets toggle — One toggle hides the idle widgets", "2026-10-09", REVIEW_STATE),
            Row("organon", "Camera jack panel trial — Camera jacks in outlined groups", "2026-10-09",
                "approved 2026-10-10: build after the title bar"),
            Row("demo", "Shortcuts — Every shortcut by category", "2026-10-09", "in review"),
            Row("—", "Plain title, a \\| in it", "2026-10-11", REVIEW_STATE),
        ])
        self.assertEqual(proposals_table.index_rows(self.root / "absent/index.md"), [])

    def test_the_index_is_in_the_folder_the_production_doc_names(self) -> None:
        home = self.write("trunk/docs/plans/home-production.md", "## Production Context\n\n- **Proposals:** `~/notes/proposals` — one row each\n")
        inside = self.write("trunk/docs/plans/inside-production.md", "- **Proposals:** docs/proposals\n")
        none = self.write("trunk/docs/plans/none-production.md", "- **UX guide:** `~/notes/ux`\n")
        self.assertEqual(proposals_table.proposals_index(home), Path.home() / "notes/proposals/index.md")
        self.assertEqual(proposals_table.proposals_index(inside), self.root / "trunk/docs/proposals/index.md")
        self.assertIsNone(proposals_table.proposals_index(none))

    def test_an_add_on_title_is_read_in_each_way_units_write_it(self) -> None:
        pending = self.write("run/next_items_pending.md", "\n".join([
            "- Action: add",
            "  Title: A second ssh machine gets its own lock storage",
            "  Target: `scripts/mac_test/mac_test.py`",
            "  Class: gate",
            "- **A distinct failure reason for a stopped run**",
            "  - Action: add",
            "  - Target: `scripts/hana_shot/hana_shot.py`",
            "- add · Periodic app-server sweep · target: a 30-minute timer",
            "",
            "- add: Archive each run's roster",
            "- **add: Find why the hook misses failures** (gotcha): details",
            "- Add a retry to the fetch",
            "- **add** — The startup word shows on a cold start",
            "  - Why needed: on a cold start the word never showed.",
            "- Action: add · Class: gate · Source: Phase 12",
            "  Title: Selecting a new tool frames it the same way as the frame key",
            "  Need: two callers still use their own framing.",
            "- Action: change",
            "  Target: `scripts/production/units_table.py`",
            "  Proposed: The units table names the phase a unit waits on. It reads the plan.",
            "- **remove**",
            "  - Why: The old footer line repeats the new one! Nothing reads it.",
            "- Action: add",
            "  Class: gate",
        ]))
        os.utime(pending, (1_760_000_000, 1_760_000_000))
        date = proposals_table.file_date(pending)
        self.assertEqual(proposals_table.add_on_rows("build-report-unit", pending), [
            Row("build-report-unit", title, date, ADD_ON_STATE) for title in (
                "A second ssh machine gets its own lock storage",
                "A distinct failure reason for a stopped run",
                "Periodic app-server sweep",
                "Archive each run's roster",
                "Find why the hook misses failures",
                "Add a retry to the fetch",
                "The startup word shows on a cold start",
                "Selecting a new tool frames it the same way as the frame key",
                "The units table names the phase a unit waits on",
                "The old footer line repeats the new one!",
                "an add-on with no description",
            )
        ])

    def test_an_empty_or_missing_pending_list_gives_no_rows(self) -> None:
        empty = self.write("run/next_items_pending.md", "")
        self.assertEqual(proposals_table.add_on_rows("unit", empty), [])
        self.assertEqual(proposals_table.add_on_rows("unit", self.root / "absent.md"), [])

    def test_a_units_add_ons_are_found_after_its_plan_moved_to_as_built(self) -> None:
        plan = self.root / "unit/docs/as-built/footers.md"
        earlier = self.root / "unit/docs/plans/footers.md"
        _ = self.write("sessions/run-1/next_items_pending.md", "- add: Keep the old footer for one release\n")
        with mock.patch.object(proposals_table, "SESSION_ROOTS", (self.root / "sessions",)), \
                self.runs_of({earlier: [Path("/history/runs/run-1.jsonl")]}):
            rows = proposals_table.unit_add_on_rows("footer-unit", plan)
        self.assertEqual([row.proposal for row in rows], ["Keep the old footer for one release"])

    def test_add_ons_are_found_under_either_run_folder_name(self) -> None:
        plan = self.root / "unit/docs/plans/footers.md"
        _ = self.write("unit-runs/run-new/next_items_pending.md", "- add: New-folder add-on\n")
        _ = self.write("delegate-runs/run-old/next_items_pending.md", "- add: Former-folder add-on\n")
        with mock.patch.object(proposals_table, "SESSION_ROOTS", (self.root / "unit-runs", self.root / "delegate-runs")), \
                self.runs_of({plan: [Path("/history/runs/run-new.jsonl"), Path("/history/runs/run-old.jsonl")]}):
            rows = proposals_table.unit_add_on_rows("footer-unit", plan)
        self.assertEqual(sorted(row.proposal for row in rows), ["Former-folder add-on", "New-folder add-on"])

    def test_the_table_keeps_one_line_per_row_and_cuts_a_long_title(self) -> None:
        long_title = "word " * 40
        text = proposals_table.table([Row("unit | one", "first\nsecond", "2026-10-12", REVIEW_STATE),
                                      Row("unit", long_title, "2026-10-12", long_title)])
        lines = text.splitlines()
        self.assertEqual(lines[0], "| Proposed by | Proposal | Date | State |")
        self.assertEqual(lines[2], "| unit \\| one | first second | 2026-10-12 | waiting for your review |")
        cells = lines[3].removesuffix(" |").split(" | ")
        self.assertTrue(cells[1].endswith("…") and cells[3].endswith("…"))
        self.assertLessEqual(len(cells[1]), proposals_table.TITLE_WIDTH)
        self.assertLessEqual(len(cells[3]), proposals_table.STATE_WIDTH)

    def test_no_state_asks_the_user_a_bare_next(self) -> None:
        states = [ADD_ON_STATE, REVIEW_STATE, *proposals_table.BACKLOG_STATES.values()]
        for state in states:
            self.assertNotIn("next", state.split())

    def run_main(self, arguments: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = proposals_table.main(arguments)
        return code, out.getvalue(), err.getvalue()

    def test_main_prints_the_four_sources_in_order(self) -> None:
        doc = self.write("trunk/docs/plans/footers-production.md", "# Production\n\n- **Proposals:** `proposals`\n")
        _ = self.write("trunk/proposals/index.md", "\n".join([
            "| Date | Proposal | Unit | Summary | Status |",
            "| --- | --- | --- | --- | --- |",
            "| 2026-10-09 | [Footer colours](footer-colours.md) | footer | One colour per state | waiting for review |",
        ]))
        plan = self.write("unit/docs/plans/footers.md", "# Plan\n")
        _ = self.write("backlog/ledger.md", "- 2026-10-11 Codex dots — proposed\n")
        _ = self.write("unit/docs/plans/proposal-pools.md", "# Proposal: reviewer pools\n\nWritten 2026-10-10 by a research agent.\n")
        _ = self.write("sessions/run-1/next_items_pending.md", "- add: Keep the old footer\n")
        records = '{"footer-unit": {"plan": "%s"}, "idle-unit": {"unavailable": "no plan"}}' % plan
        with mock.patch.object(proposals_table, "BACKLOG_DIR", self.root / "backlog"), \
                mock.patch.object(proposals_table, "SESSION_ROOTS", (self.root / "sessions",)), \
                mock.patch.object(phase_table, "show_production", return_value=records), \
                self.runs_of({plan: [Path("run-1.jsonl")]}):
            code, out, err = self.run_main(["--production", str(doc)])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([line.split(" | ")[1] for line in out.splitlines()[2:]],
                         ["Footer colours — One colour per state", "Codex dots", "reviewer pools", "Keep the old footer"])
        self.assertEqual([line.removesuffix(" |").split(" | ")[3] for line in out.splitlines()[2:]],
                         [REVIEW_STATE, REVIEW_STATE, REVIEW_STATE, ADD_ON_STATE])

    def test_main_says_so_when_nothing_is_open(self) -> None:
        doc = self.write("trunk/docs/plans/footers-production.md", "# Production\n")
        with mock.patch.object(proposals_table, "BACKLOG_DIR", self.root / "backlog"), \
                mock.patch.object(phase_table, "show_production", return_value="{}"):
            self.assertEqual(self.run_main(["--production", str(doc)]), (0, "no open proposals\n", ""))

    def test_main_refuses_a_session_that_runs_no_production(self) -> None:
        with mock.patch.object(showrunners, "production_of", return_value=None):
            code, out, err = self.run_main([])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("pass --production <doc>", err)

    def test_main_rejects_other_arguments(self) -> None:
        code, _, err = self.run_main(["--everything"])
        self.assertEqual(code, 2)
        self.assertIn("usage: proposals_table.py", err)


if __name__ == "__main__":
    _ = unittest.main()
