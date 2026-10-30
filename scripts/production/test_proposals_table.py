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

import issues
import proposals_folder
import proposals_table
import showrunners
import showrunner_tree
from proposals_table import ADD_ON_STATE, IN_PROGRESS_STATE, REVIEW_STATE, Row

# Importable once proposals_table has put scripts/direct on the path.
import phase_table
from scripts.direct import plan_history


@final
class ProposalsTableTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        # No live update timer names a production below the test's own.
        _ = self.enterContext(mock.patch.object(showrunners, "timer_docs", return_value={}))
        _ = self.enterContext(mock.patch.object(showrunner_tree, "descendants_of", return_value=[]))

    @contextlib.contextmanager
    def runs_of(self, runs: dict[Path, list[Path]]) -> Generator[None]:
        """Give each plan path the run files named for it, and every other path none."""
        def plan_runs(plan: Path) -> list[Path]:
            return runs.get(plan, [])
        with mock.patch.object(plan_history, "plan_runs", side_effect=plan_runs):
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

    def issue(self, name: str, *fields: str, folder: str = "issues") -> Path:
        """An issue file whose frontmatter holds the given lines, with a body below it."""
        return self.write(f"{folder}/{name}.md", "\n".join(["---", *fields, "---", "Body text.", ""]))

    def test_the_table_lists_waiting_proposals_before_in_progress_ones(self) -> None:
        _ = self.issue("selection widgets toggle", 'date_created: "[[2026-10-09]]"', 'project: "[[hana]]"',
                       "stage: backlog", "status: open", "tags:", "  - issue", "  - proposal",
                       "proposed_by: organon-unit", 'plan: ""', 'proposal_id: "P3"')
        _ = self.issue("camera jack panel", 'date_created: "[[2026-10-10]]"', "stage: backlog", "status: open",
                       "tags: [issue, proposal]", "proposed_by: '[[demo-unit]]'",
                       "plan: docs/plans/jacks.md Phase 4–6", "unit: demo-unit")
        _ = self.issue("rich text", "date_created: 2026-10-11", "stage: active", "status: open",
                       "tags: [issue, proposal]", "proposed_by:", "unit: widget-unit")
        _ = self.issue("unowned build", "date_created: 2026-10-12", "stage: active", "status: open",
                       'tags: ["issue", "proposal"]', "unit: ")
        _ = self.issue("late idea", "date_created: 2026-10-13", "stage: backlog", "status: open",
                       "tags: [issue, proposal]", "proposed_by: organon-unit", "plan: docs/plans/late.md")
        self.assertEqual(proposals_table.issue_rows(issues.open_issues(self.root / "issues", "proposal")), [
            Row("organon-unit", "P3 selection widgets toggle", "2026-10-09", REVIEW_STATE),
            Row("organon-unit", "late idea", "2026-10-13", REVIEW_STATE),
            Row("demo-unit", "camera jack panel", "2026-10-10", "in progress by demo-unit"),
            Row("—", "rich text", "2026-10-11", "in progress by widget-unit"),
            Row("—", "unowned build", "2026-10-12", IN_PROGRESS_STATE),
        ])

    def test_waiting_proposals_go_by_urgency_then_score_and_next_keeps_the_shown_one(self) -> None:
        common = ("stage: backlog", "status: open", "tags: [issue, proposal]", "proposed_by: organon-unit")
        _ = self.issue("unscored", "date_created: 2026-10-01", *common)
        low = self.issue("low score", "date_created: 2026-10-02", *common)
        _ = low.write_text(low.read_text() + "\nProposed ranking: goal 1 - Ship Hana · alignment ⭐ · impact ⭐ · urgency ⭐ · effort ⭐\n")
        _ = self.issue("high score", "date_created: 2026-10-03", *common, 'backlog_goal: "1 - Ship Hana"',
                       'backlog_alignment: "⭐⭐⭐"', 'backlog_impact: "⭐⭐"', 'backlog_urgency: "⭐"', 'backlog_effort: "⭐"')
        _ = self.issue("blocking", "date_created: 2026-10-04", *common, 'review_urgency: "blocks"',
                       'depends_on: ["[[unscored]]"]')
        found = issues.open_issues(self.root / "issues", "proposal")
        self.assertEqual([row.proposal for row in proposals_table.issue_rows(found)],
                         ["unscored", "blocking", "high score", "low score"])
        self.assertEqual(proposals_table.issue_rows(found)[1].state, f"{REVIEW_STATE} · blocks")
        doc = self.write("trunk/docs/plans/demo-production.md", f"- **Proposals:** `{self.root / 'issues'}`\n")
        with mock.patch.dict(os.environ, {"NEXT_STATE_DIR": str(self.root / "next")}), \
                mock.patch.object(proposals_table, "owners", return_value=frozenset({"organon-unit"})):
            self.assertEqual(self.run_main(["--production", str(doc), "--next"])[1].splitlines()[0], "unscored")
            _ = self.issue("now important", "date_created: 2026-10-05", *common, 'review_urgency: "user"')
            # The shown proposal stays until the user answers it.
            self.assertEqual(self.run_main(["--next", "--production", str(doc)])[1].splitlines()[0], "unscored")
            (self.root / "issues/unscored.md").unlink()
            self.assertEqual(self.run_main(["--production", str(doc), "--next"])[1].splitlines(),
                             ["now important", str(self.root / "issues/now important.md")])

    def test_a_production_reads_its_own_folder_and_queue_and_with_below_each_lower_one_never_above(self) -> None:
        docs: dict[str, Path] = {}
        for slug in ("top", "kid", "grand"):
            _ = self.issue(f"{slug} idea", "date_created: 2026-10-01", "status: open", "tags: [issue, proposal]",
                           f"proposed_by: {slug}", folder=f"{slug}-issues")
            _ = self.write(f"{slug}-backlog/ledger.md", f"- 2026-10-02 {slug} line — proposed\n")
            docs[slug] = self.write(f"trunk/docs/plans/{slug}-production.md",
                                    f"- **Proposals:** `{self.root / f'{slug}-issues'}`\n"
                                    + f"- **Improvement queue:** {self.root / f'{slug}-backlog' / 'ledger.md'}\n")
        below = {"top": ["top", "kid", "grand"], "kid": ["kid", "grand"], "grand": ["grand"]}

        def owner_docs(doc: Path) -> list[Path]:
            return [docs[slug] for slug in below[doc.name.removesuffix("-production.md")]]

        def waiting(slug: str, below: bool = False) -> list[str]:
            return [entry.item.proposal for entry in proposals_table.waiting_entries(docs[slug], below)[0]]

        with mock.patch.object(proposals_folder, "owner_docs", owner_docs):
            # Each showrunner reviews its own production; the ones below review theirs (user, 2026-10-27).
            self.assertEqual(waiting("top"), ["top idea", "top line"])
            self.assertEqual(waiting("kid"), ["kid idea", "kid line"])
            self.assertEqual(waiting("grand"), ["grand idea", "grand line"])
            self.assertEqual(waiting("top", below=True),
                             ["grand idea", "kid idea", "top idea", "top line", "kid line", "grand line"])
            self.assertEqual(waiting("kid", below=True), ["grand idea", "kid idea", "kid line", "grand line"])

    def test_closed_untagged_and_subfolder_issues_give_no_rows(self) -> None:
        _ = self.issue("built one", "status: closed", "tags: [issue, proposal]", "as_built: docs/hana/as-built/x.md")
        _ = self.issue("plain issue", "status: open", "tags:", "  - issue")
        _ = self.issue("research notes", "status: open", "tags: [issue, proposal]", folder="issues/rich text")
        _ = self.write("issues/no frontmatter.md", "status: open\ntags: [proposal]\n")
        _ = self.write("issues/unclosed.md", "---\nstatus: open\ntags: [proposal]\n")
        self.assertEqual(proposals_table.issue_rows(issues.open_issues(self.root / "issues", "proposal")), [])
        self.assertEqual(proposals_table.issue_rows(issues.open_issues(self.root / "absent", "proposal")), [])

    def test_a_wiki_link_value_reads_as_its_alias_or_target(self) -> None:
        self.assertEqual([issues.plain(value) for value in (
            '"[[2026-03-03]]"', "'[[hana]]'", '"[[issue structure#enhancement|enhancement]]"', "backlog", '""',
        )], ["2026-03-03", "hana", "enhancement", "backlog", ""])

    def test_the_issues_folder_is_the_one_the_production_doc_names(self) -> None:
        home = self.write("trunk/docs/plans/home-production.md", "## Production Context\n\n- **Proposals:** `~/notes/issues` — tagged proposal\n")
        inside = self.write("trunk/docs/plans/inside-production.md", "- **Proposals:** docs/issues\n")
        none = self.write("trunk/docs/plans/none-production.md", "- **UX guide:** `~/notes/ux`\n")
        self.assertEqual(proposals_folder.issues_folder(home), Path.home() / "notes/issues")
        self.assertEqual(proposals_folder.issues_folder(inside), self.root / "trunk/docs/issues")
        self.assertIsNone(proposals_folder.issues_folder(none))

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
        _ = self.write("former-runs/run-old/next_items_pending.md", "- add: Former-folder add-on\n")
        with mock.patch.object(proposals_table, "SESSION_ROOTS", (self.root / "unit-runs", self.root / "former-runs")), \
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

    def test_main_prints_the_three_sources_in_order(self) -> None:
        doc = self.write("trunk/docs/plans/footers-production.md", "# Production\n\n- **Proposals:** `issues`\n"
                         + f"- **Improvement queue:** {self.root / 'backlog' / 'ledger.md'}\n\n"
                         + "## Units\n\n| Unit | Plan |\n| --- | --- |\n| footer-unit | `docs/plans/footers.md` |\n")
        _ = self.issue("footer colours", "date_created: 2026-10-09", "status: open", "tags: [issue, proposal]",
                       "proposed_by: footer-unit", folder="trunk/issues")
        # Another production's proposal in the same folder is not this one's.
        _ = self.issue("style rule", "date_created: 2026-10-09", "status: open", "tags: [issue, proposal]",
                       "proposed_by: cargo-liner-style", folder="trunk/issues")
        plan = self.write("unit/docs/plans/footers.md", "# Plan\n")
        _ = self.write("backlog/ledger.md", "- 2026-10-11 Codex dots — proposed\n")
        _ = self.write("sessions/run-1/next_items_pending.md", "- add: Keep the old footer\n")
        records = '{"footer-unit": {"plan": "%s"}, "idle-unit": {"unavailable": "no plan"}}' % plan
        with mock.patch.object(proposals_table, "SESSION_ROOTS", (self.root / "sessions",)), \
                mock.patch.object(phase_table, "show_production", return_value=records), \
                self.runs_of({plan: [Path("run-1.jsonl")]}):
            code, out, err = self.run_main(["--production", str(doc)])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([line.split(" | ")[1] for line in out.splitlines()[2:]],
                         ["footer colours", "Codex dots", "Keep the old footer"])
        self.assertEqual([line.removesuffix(" |").split(" | ")[3] for line in out.splitlines()[2:]],
                         [REVIEW_STATE, REVIEW_STATE, ADD_ON_STATE])

    def test_main_says_so_when_nothing_is_open(self) -> None:
        doc = self.write("trunk/docs/plans/footers-production.md", "# Production\n")
        with mock.patch.object(phase_table, "show_production", return_value="{}"):
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
