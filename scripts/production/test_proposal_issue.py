#!/usr/bin/env python3
"""Tests for proposal_issue.py: the proposal verbs that edit one issue's frontmatter."""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from typing import final, override
from unittest import mock

import proposal_issue
from user_zone import user_zone

ISSUE = """---
area: "[[tooling]]"
category:
  - "[[issue structure#feature|feature]]"
date_created: "[[2026-10-01]]"
date_modified: "[[2026-10-01]]"
plan: ""
project: "[[hana]]"
proposed_by: "build-report"
return:
  - "[[issues - hana.base]]"
  - "[[issues.base]]"
stage: backlog
status: open
tags:
- issue
- proposal
---
# body

status: open stays in the body
"""


@final
class ProposalIssueTests(unittest.TestCase):
    root: Path = Path()
    issue: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.issue = self.root / "a proposal.md"
        _ = self.issue.write_text(ISSUE, encoding="utf-8")
        self.enterContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}))

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = proposal_issue.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def today(self) -> str:
        return f'"[[{datetime.now(user_zone()).date().isoformat()}]]"'

    def text(self) -> str:
        return self.issue.read_text(encoding="utf-8")

    def test_approve_sets_three_fields_and_keeps_stage(self) -> None:
        code, out, _ = self.run_cli(
            "approve", str(self.issue), "--showrunner", "hana", "--unit", "build-report",
            "--plan", "docs/plans/x.md Phase 4–6",
        )
        self.assertEqual(code, 0)
        text = self.text()
        self.assertIn('showrunner: "hana"\n', text)
        self.assertIn('unit: "build-report"\n', text)
        self.assertIn('plan: "docs/plans/x.md Phase 4–6"\n', text)
        self.assertIn("stage: backlog\n", text)
        self.assertIn(f"date_modified: {self.today()}\n", text)
        self.assertEqual(out, "approve a proposal.md: set showrunner, unit, plan, date_modified\n")

    def test_insert_goes_before_the_closing_fence_and_replace_stays_in_place(self) -> None:
        _ = self.run_cli(
            "approve", str(self.issue), "--showrunner", "hana", "--unit", "u", "--plan", "p Phase 1",
        )
        lines = self.text().splitlines()
        fence = lines.index("---", 1)
        self.assertEqual(lines[fence - 2:fence], ['showrunner: "hana"', 'unit: "u"'])
        self.assertEqual(lines.index('plan: "p Phase 1"'), ISSUE.splitlines().index('plan: ""'))

    def test_a_home_relative_path_is_expanded(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            code, _, _ = self.run_cli("start", "~/a proposal.md")
        self.assertEqual(code, 0)
        self.assertIn("stage: active\n", self.text())

    def test_start_sets_stage_active(self) -> None:
        code, out, _ = self.run_cli("start", str(self.issue))
        self.assertEqual(code, 0)
        self.assertIn("stage: active\n", self.text())
        self.assertEqual(out, "start a proposal.md: set stage, date_modified\n")

    def test_close_sets_as_built_status_and_dates(self) -> None:
        code, _, _ = self.run_cli("close", str(self.issue), "--as-built", "docs/hana/as-built/x.md")
        self.assertEqual(code, 0)
        text = self.text()
        self.assertIn('as_built: "docs/hana/as-built/x.md"\n', text)
        self.assertIn("status: closed\n", text)
        self.assertIn(f"date_closed: {self.today()}\n", text)
        self.assertIn("status: open stays in the body\n", text)

    def test_stars_writes_quoted_stars_from_counts_or_stars(self) -> None:
        code, _, _ = self.run_cli(
            "stars", str(self.issue), "--goal", "1 - Ship Hana", "--alignment", "4",
            "--impact", "⭐⭐⭐", "--urgency", "1", "--effort", "5",
        )
        self.assertEqual(code, 0)
        text = self.text()
        for line in (
            'backlog_goal: "1 - Ship Hana"', 'backlog_alignment: "⭐⭐⭐⭐"', 'backlog_impact: "⭐⭐⭐"',
            'backlog_urgency: "⭐"', 'backlog_effort: "⭐⭐⭐⭐⭐"',
        ):
            self.assertIn(line + "\n", text)

    def test_stars_refuses_values_out_of_range(self) -> None:
        for flag, value in (("--alignment", "0"), ("--impact", "6"), ("--effort", "⭐⭐⭐⭐⭐⭐"), ("--goal", "Ship")):
            argv = {"--goal": "1 - Ship Hana", "--alignment": "3", "--impact": "3", "--urgency": "1", "--effort": "2"}
            argv[flag] = value
            code, _, err = self.run_cli("stars", str(self.issue), *[part for pair in argv.items() for part in pair])
            self.assertEqual(code, 2, flag)
            self.assertIn(flag, err)
            self.assertEqual(self.text(), ISSUE)

    def test_block_list_value_is_replaced_with_its_continuation_lines(self) -> None:
        text, changed = proposal_issue.set_fields(ISSUE, {"tags": "proposal"}, self.issue)
        self.assertEqual(changed, ["tags"])
        self.assertIn('status: open\ntags: "proposal"\n---\n', text)
        self.assertNotIn("- issue\n", text)

    def test_other_lines_stay_byte_identical(self) -> None:
        crlf = ISSUE.replace("\n", "\r\n")
        _ = self.issue.write_bytes(crlf.encode("utf-8"))
        changed = proposal_issue.edit(self.issue, {"stage": "active"}, date(2026, 10, 15))
        self.assertEqual(changed, ["stage", "date_modified"])
        after = self.issue.read_bytes().decode("utf-8").split("\r\n")
        before = crlf.split("\r\n")
        differing = [index for index, (old, new) in enumerate(zip(before, after, strict=True)) if old != new]
        self.assertEqual([after[index] for index in differing], ['date_modified: "[[2026-10-15]]"', "stage: active"])

    def test_second_identical_edit_does_not_write(self) -> None:
        _ = proposal_issue.edit(self.issue, {"stage": "active"}, date(2026, 10, 15))
        stamp = self.issue.stat().st_mtime_ns
        self.assertEqual(proposal_issue.edit(self.issue, {"stage": "active"}, date(2026, 10, 15)), [])
        self.assertEqual(self.issue.stat().st_mtime_ns, stamp)

    def test_refuses_a_missing_file(self) -> None:
        code, out, err = self.run_cli("start", str(self.root / "gone.md"))
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("gone.md is not a file", err)

    def test_refuses_a_file_without_frontmatter(self) -> None:
        for body in ("# no frontmatter\n", "---\nstatus: open\n"):
            _ = self.issue.write_text(body, encoding="utf-8")
            code, _, err = self.run_cli("start", str(self.issue))
            self.assertEqual(code, 2)
            self.assertIn("frontmatter", err)
            self.assertEqual(self.text(), body)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["a proposal.md"])


if __name__ == "__main__":
    _ = unittest.main()
