#!/usr/bin/env python3
"""Tests for proposal_issue.py: the proposal verbs that edit one issue's frontmatter."""
from __future__ import annotations

import contextlib
import fcntl
import io
import threading
import time
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from typing import final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    runtime: Path = Path()
    issue: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.issue = self.root / "a proposal.md"
        _ = self.issue.write_text(ISSUE, encoding="utf-8")
        self.runtime = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York", "XDG_RUNTIME_DIR": str(self.runtime)}))

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = proposal_issue.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def today(self) -> str:
        return f'"[[{datetime.now(user_zone()).date().isoformat()}]]"'

    def text(self) -> str:
        return self.issue.read_text(encoding="utf-8")

    def test_create_fills_template_and_protects_the_decision_key(self) -> None:
        template = self.root / "issue.yaml"
        _ = template.write_text("---\narea: \"[[{{AREA}}]]\"\nproject: \"[[{{PROJECT}}]]\"\n"
                                + "category: [\"[[issue structure#{{CATEGORY}}|{{CATEGORY}}]]\"]\n"
                                + "proposed_by: \"{{PROPOSED_BY}}\"\nbacklog_goal: \"{{BACKLOG_GOAL}}\"\n"
                                + "date_created: \"[[{{DATE}}]]\"\nstatus: open\ntags: [issue]\n---\n")
        body = self.root / "body.md"
        _ = body.write_text("# Findings\nBefore and after.\n")
        issue = self.root / "new style issue.md"
        arguments = ["create", str(issue), "--project", "alpha", "--short", "alpha", "--proposed-by", "sample-style",
                     "--extra-tags", "style", "--rule", "global:rust/names.md", "--crate", "alpha", "--rule-blob", "abc123", "--count", "3", "--body-file", str(body), "--template", str(template)]
        ranking = "goal 1 - Ship Hana · alignment ⭐⭐ · impact ⭐⭐ · urgency ⭐ · effort ⭐"
        for label, flags, expected in (("new style issue", [], ""),
                                       ("ranked style issue", ["--proposed-ranking", ranking], ranking)):
            with self.subTest(ranking=expected):
                destination = self.root / (label + ".md")
                command = [*arguments]
                command[1] = str(destination)
                code, _, err = self.run_cli(*command, *flags)
                self.assertEqual(code, 0, err)
                self.assertIn(f"\n\nProposed ranking: {expected}\n\n<!-- style-decision:start -->\n# Findings\nBefore and after.\n",
                              destination.read_text())
        from issues import frontmatter
        fields = frontmatter(issue)
        self.assertEqual(fields["tags"], ["issue", "proposal", "style"])
        self.assertNotIn("style_findings", fields)
        self.assertEqual(fields["style_count"], ["3"])
        self.assertEqual(fields["area"], ["unfiled"])
        self.assertEqual(fields["category"], ["refactor"])
        self.assertEqual(fields["backlog_goal"], [])
        self.assertEqual(fields["project"], ["alpha"])
        self.assertTrue(issue.read_text().split("---", 2)[2].lstrip().startswith("Proposed ranking:"))
        original, stamp = issue.read_bytes(), issue.stat().st_mtime_ns
        self.assertEqual(self.run_cli(*arguments)[0], 0)
        self.assertEqual(issue.stat().st_mtime_ns, stamp)
        arguments[arguments.index("--rule-blob") + 1] = "c"
        self.assertEqual(self.run_cli(*arguments)[0], 2)
        self.assertEqual(issue.read_bytes(), original)

    def test_create_with_rules_preserves_existing_decision_and_answer(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        body = self.root / "body.md"
        _ = body.write_text("# Keep rule lines\nRule: global:rust/names.md\n")
        issue = self.root / "linked.md"
        urls = "https://github.com/o/r/blob/main/b.md,https://github.com/o/r/blob/main/a.md"
        args = ["create", str(issue), "--project", "sample", "--short", "s", "--proposed-by", "style",
                "--rule", "global:rust/names.md", "--crate", "alpha", "--rule-blob", "abc123", "--count", "1", "--rules", urls, "--template", str(template), "--body-file", str(body)]
        code, _, error = self.run_cli(*args)
        self.assertEqual(code, 0, error)
        from issues import frontmatter
        self.assertEqual(frontmatter(issue)["style_rules"], sorted(urls.split(",")))
        original = issue.read_bytes()
        args[args.index("--rules") + 1] = "https://github.com/o/r/blob/main/changed.md"
        self.assertEqual(self.run_cli(*args)[0], 0)
        self.assertEqual(issue.read_bytes(), original)

    def test_refresh_updates_generated_examples_and_count_preserving_answer_and_notes(self) -> None:
        original = ISSUE.replace("---\n# body", 'unit: "names-unit"\nstyle_count: "1"\n---\n# body')
        original += "\n" + proposal_issue.decision_body("# Old examples\n") + "\nUser notes stay.\n"
        _ = self.issue.write_text(original)
        body = "# New examples\nFindings: 4\n"
        today = datetime.now(user_zone()).date()
        self.assertIn("body", proposal_issue.refresh_decision(self.issue, 4, body, "ranking", today))
        self.assertIn('unit: "names-unit"', self.text())
        self.assertIn('style_count: "4"', self.text())
        self.assertIn("# body\n\nstatus: open stays in the body", self.text())
        self.assertIn("User notes stay.", self.text())
        self.assertIn("# New examples", self.text())
        self.assertNotIn("# Old examples", self.text())
        stamp = self.issue.stat().st_mtime_ns
        self.assertEqual(proposal_issue.refresh_decision(self.issue, 4, body, "ranking", today), [])
        self.assertEqual(self.issue.stat().st_mtime_ns, stamp)

    def test_reopen_removes_every_close_field_and_keeps_the_answer(self) -> None:
        _ = proposal_issue.edit(self.issue, {"unit": "names-unit", "plan": "unit.md"}, date(2026, 10, 15))
        from issues import frontmatter
        def fields() -> dict[str, list[str]]:
            return {key: value for key, value in frontmatter(self.issue).items() if key != "date_modified"}
        before = fields()
        for closure in (["--reason", "built in names-unit-1"], ["--as-built", "docs/as-built/x.md"]):
            with self.subTest(closure=closure[0]):
                self.assertEqual(self.run_cli("close", str(self.issue), *closure)[0], 0)
                code, _, error = self.run_cli("reopen", str(self.issue))
                self.assertEqual(code, 0, error)
                self.assertEqual(fields(), before)

    def decision_issue(self) -> None:
        text = ISSUE.replace("---\n# body", 'style_count: "1"\n---\n# body') + "\nProposed ranking: effort one\n\n"
        _ = self.issue.write_text(text + proposal_issue.decision_body("# Old examples\n"))

    def test_refresh_and_approve_at_the_same_time_keep_both_changes(self) -> None:
        self.decision_issue()
        today = datetime.now(user_zone()).date()
        original = proposal_issue._write  # pyright: ignore[reportPrivateUsage]
        approval = threading.Thread(target=proposal_issue.edit, args=(self.issue, {"unit": "names-unit", "plan": "p"}, today))
        def write_after_the_approval_starts(path: Path, text: str) -> None:
            if not approval.is_alive() and "# New examples" in text:
                approval.start()
                time.sleep(0.3)
            original(path, text)
        with mock.patch.object(proposal_issue, "_write", side_effect=write_after_the_approval_starts):
            _ = proposal_issue.refresh_decision(self.issue, 2, "# New examples\n", "effort two", today)
            approval.join()
        self.assertIn('unit: "names-unit"', self.text())
        self.assertIn("# New examples", self.text())
        self.assertIn('style_count: "2"', self.text())

    def test_every_writing_verb_holds_the_issue_lock_outside_the_issue_folder(self) -> None:
        today = datetime.now(user_zone()).date()
        verbs = (("edit", lambda: proposal_issue.edit(self.issue, {"stage": "active"}, today)),
                 ("rules", lambda: proposal_issue.edit_rules(self.issue, ["https://example.invalid/rule.md"], today)),
                 ("refresh", lambda: proposal_issue.refresh_decision(self.issue, 3, "# Locked\n", "effort", today)),
                 ("reopen", lambda: proposal_issue.reopen(self.issue, today)))
        for name, verb in verbs:
            with self.subTest(verb=name):
                self.decision_issue()
                _ = proposal_issue.edit(self.issue, {"status": "closed"}, date(2026, 10, 15))
                blocked: list[bool] = []
                def write(path: Path, text: str) -> None:
                    lock, = self.runtime.glob("proposal-issue-*.lock")
                    with lock.open("a") as other:
                        try:
                            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            blocked.append(True)
                    _ = path.write_text(text)
                with mock.patch.object(proposal_issue, "_write", side_effect=write):
                    _ = verb()
                self.assertEqual(blocked, [True])
                self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["a proposal.md"])

    def test_fill_ranking_writes_an_empty_ranking_once_under_the_issue_lock(self) -> None:
        self.decision_issue()
        _ = self.issue.write_text(self.text().replace("Proposed ranking: effort one", "Proposed ranking: "))
        blocked: list[bool] = []
        def write(path: Path, text: str) -> None:
            lock, = self.runtime.glob("proposal-issue-*.lock")
            with lock.open("a") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocked.append(True)
            _ = path.write_text(text)
        with mock.patch.object(proposal_issue, "_write", side_effect=write):
            self.assertTrue(proposal_issue.fill_ranking(self.issue, "effort two"))
            self.assertEqual(blocked, [True])
            self.assertIn("\nProposed ranking: effort two\n", self.text())
            before = self.issue.read_bytes()
            self.assertFalse(proposal_issue.fill_ranking(self.issue, "effort three"))
        self.assertEqual(blocked, [True])
        self.assertEqual(self.issue.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["a proposal.md"])

    def test_create_takes_inherited_fields_as_a_json_object_of_known_text_fields(self) -> None:
        template = self.root / "template.yaml"
        _ = template.write_text("---\nstatus: open\n---\n")
        body = self.root / "body.md"
        _ = body.write_text("# Examples\n")
        issue = self.root / "inherited.md"
        arguments = ["create", str(issue), "--project", "sample", "--short", "s", "--proposed-by", "style",
                     "--rule", "global:rust/names.md", "--crate", "alpha", "--rule-blob", "abc123", "--count", "1",
                     "--template", str(template), "--body-file", str(body), "--initial-fields"]
        for fields, message in (('{"status": "closed"}', "invalid inherited fields: status"),
                                ('{"unit": 1}', "unit is not text"), ('["unit"]', "JSON object")):
            with self.subTest(fields=fields):
                code, _, error = self.run_cli(*arguments, fields)
                self.assertEqual(code, 2)
                self.assertIn(message, error)
                self.assertFalse(issue.exists())
        self.assertEqual(self.run_cli(*arguments, '{"unit": "names-unit", "plan": "unit.md"}')[0], 0)
        from issues import frontmatter
        self.assertEqual((frontmatter(issue)["unit"], frontmatter(issue)["plan"]), (["names-unit"], ["unit.md"]))

    def test_rules_sets_list_preserving_other_keys_and_body_bytes(self) -> None:
        url = "https://github.com/o/r/blob/main/rust/names.md"
        for existing in ("", 'style_rules: ["old"]\n'):
            with self.subTest(existing=existing):
                original = ISSUE.replace("---\n# body", existing + "---\n# body").replace("\n", "\r\n")
                _ = self.issue.write_bytes(original.encode())
                code, _, error = self.run_cli("rules", str(self.issue), "--rules", url)
                self.assertEqual(code, 0, error)
                expected = original.replace('date_modified: "[[2026-10-01]]"', f"date_modified: {self.today()}")
                line = f'style_rules: ["{url}"]\r\n'
                expected = expected.replace(existing.replace("\n", "\r\n"), line) if existing else expected.replace("---\r\n# body", line + "---\r\n# body")
                self.assertEqual(self.issue.read_bytes(), expected.encode())
                stamp = self.issue.stat().st_mtime_ns
                self.assertEqual(self.run_cli("rules", str(self.issue), "--rules", url)[0], 0)
                self.assertEqual(self.issue.stat().st_mtime_ns, stamp)

    def test_close_accepts_a_reason_instead_of_an_as_built_path(self) -> None:
        code, _, err = self.run_cli("close", str(self.issue), "--reason", "declined: keep the API")
        self.assertEqual(code, 0, err)
        self.assertIn('reason: "declined: keep the API"', self.text())
        self.assertIn("status: closed", self.text())
        self.assertNotIn("as_built:", self.text())

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
