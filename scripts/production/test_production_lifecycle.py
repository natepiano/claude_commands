"""Exercise production lifecycle steps in disposable git repositories."""

from __future__ import annotations

import contextlib
from dataclasses import replace
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import final, override
from unittest import mock

import fake_unit_host
import production_lifecycle
import showrunners
import showrunner_tree
import showrunner_parent
from production_units import read_production
from fake_unit_host import HostedUnit


from scripts.production import production_doc, staff
from scripts.production.production_lifecycle_test_support import LifecycleFixture, SCRIPT


@final
class ProductionNameTests(unittest.TestCase):
    def test_shared_reader_returns_the_trimmed_heading_name(self) -> None:
        lines = ["# Another document", "# Production —  example  ", "# Production — later"]
        self.assertEqual(production_doc.production_name(lines), production_doc.ProductionName("example"))
        self.assertEqual(production_lifecycle.production_name(lines), "example")

    def test_shared_reader_reports_a_missing_production_heading(self) -> None:
        self.assertEqual(production_doc.production_name(["# Another document"]),
                         production_doc.ProductionNameNotRecorded("production doc lacks its name"))

    def test_empty_heading_is_a_recorded_name_and_remains_valid_for_lifecycle(self) -> None:
        lines = ["# Production — "]
        self.assertEqual(production_doc.production_name(lines), production_doc.ProductionName(""))
        self.assertEqual(production_lifecycle.production_name(lines), "")

    def test_lifecycle_missing_heading_keeps_its_stop_fields(self) -> None:
        with self.assertRaises(production_lifecycle.LifecycleStop) as stopped:
            _ = production_lifecycle.production_name(["# Another document"])
        self.assertEqual((stopped.exception.step, stopped.exception.state, stopped.exception.detail),
                         ("input", "failed", "production doc lacks its name"))


@final
class LifecycleTests(LifecycleFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.staff.write_document({"schema": 1, "rows": []})
        _ = self.enterContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.state / "notifier"))

    @override
    def reports_to(self, child: str, parent: str) -> Path:
        path = super().reports_to(child, parent)
        if "example" in (child, parent):
            self.write(self.state / "notifier/showrunner-example/conf",
                       f"TARGET=session:example-session\nCHECK=zsh /x/production_check.sh {self.doc}\n")
        rows = {row.name: row for row in staff.find(kind="showrunner")}
        parent_name = "old-showrunner" if parent == "example" else parent
        child_name = "old-showrunner" if child == "example" else child
        above = rows.get(parent_name)
        if above is None:
            above = staff.ensure("showrunner", name=parent_name)
        below = rows.get(child_name)
        if below is None:
            _ = staff.ensure("showrunner", name=child_name, parent=above.id)
        elif below.parent != above.id:
            _ = staff.move(below.id, above.id)
        return path

    def test_wrap_child_check_follows_staff_parent_despite_stale_parent_file(self) -> None:
        self.merged_units()
        self.showrunner("kid", live=True)
        _ = self.reports_to("kid", "example")
        child = next(row for row in staff.find(kind="showrunner") if row.name == "kid")
        _ = staff.move(child.id, None)
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertEqual((self.state / "showrunner/kid/parent").read_text(), "example\n")

    def staff_tree(self) -> tuple[staff.StaffRow, staff.StaffRow, staff.StaffRow, staff.StaffRow]:
        self.merged_units()
        top = staff.ensure("showrunner", name="top")
        runner = staff.ensure("showrunner", name="old-showrunner", parent=top.id)
        alpha = staff.ensure("unit", name="alpha", parent=runner.id)
        beta = staff.ensure("unit", name="beta", parent=runner.id)
        child = staff.ensure("showrunner", name="kid", parent=runner.id)
        for slug in ("top", "kid"):
            self.showrunner(slug, live=slug == "top")
        _ = self.reports_to("example", "top")
        _ = self.reports_to("kid", "example")
        return runner, alpha, beta, child

    def finish_interrupted_wrap(self, retire_showrunner: bool) -> None:
        runner, alpha, beta, child = self.staff_tree()
        target = runner.id if retire_showrunner else alpha.id
        original = staff.retire
        def interrupted(staff_id: staff.StaffId) -> staff.StaffRow | None:
            row = original(staff_id)
            if staff_id == target:
                raise OSError("stopped after staff retirement")
            return row
        with mock.patch.object(staff, "retire", side_effect=interrupted):
            first = self.run_lifecycle("wrap", "--no-ci")
        self.assertEqual(first.returncode, 2, first.stdout + first.stderr)
        self.assertTrue((self.state / "showrunner/example/wrap_release.json").exists())
        with mock.patch.object(showrunner_tree, "edges_to_release", side_effect=AssertionError("capture read twice")):
            result = self.run_lifecycle("wrap", "--no-ci")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for row in (runner, alpha, beta):
            self.assertIsNone(staff.by_id(row.id))
        self.assertEqual(staff.by_id(child.id), replace(child, parent=None))
        self.assertEqual(len(self.sent()), 1)
        data, document = self.staff.staff_file.read_bytes(), self.doc.read_bytes()
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.doc.read_bytes()), (data, document))
        self.assertEqual(len(self.sent()), 1)

    def test_wrap_rerun(self) -> None:
        self.finish_interrupted_wrap(False)

    def test_wrap_rerun_after_showrunner_retirement_keeps_the_release_capture(self) -> None:
        self.finish_interrupted_wrap(True)

    def test_wrap_with_workers_retires_pending_ids_on_an_already_wrapped_retry(self) -> None:
        runner, alpha, beta, child = self.staff_tree()
        worker = staff.ensure("worker", name="impl1", parent=alpha.id)
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(worker.id, result.stdout)
        self.assertEqual(staff.by_id(alpha.id), alpha)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertIsNone(staff.by_id(beta.id))
        self.assertEqual(staff.by_id(child.id), replace(child, parent=None))
        self.assertEqual(staff.snapshot().pending_retire, tuple(sorted((runner.id, alpha.id))))
        self.assertIn("wrapped", self.doc.read_text())
        _ = staff.retire(worker.id)
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(staff.by_id(alpha.id))
        self.assertIsNone(staff.by_id(runner.id))
        self.assertEqual(staff.snapshot().pending_retire, ())

    def permanent_staff_tree(self) -> tuple[staff.StaffRow, staff.StaffRow, staff.StaffRow, staff.StaffRow, staff.StaffRow]:
        runner, alpha, beta, child = self.staff_tree()
        permanent = staff.ensure("unit", name="nightly", parent=runner.id)
        text = self.doc.read_text().replace("## Close-out", "| nightly-unit | permanent | /not-here | nightly | — | — | — |\n\n## Close-out")
        self.write(self.doc, text)
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "record permanent unit")
        return runner, alpha, beta, child, permanent

    def test_permanent_wrap_keeps_unit_and_showrunner_but_roots_every_showrunner_edge(self) -> None:
        runner, alpha, beta, child, permanent = self.permanent_staff_tree()
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(permanent.name, result.stdout)
        self.assertIn(runner.name, result.stdout)
        self.assertEqual(staff.by_id(permanent.id), permanent)
        self.assertIsNone(staff.by_id(alpha.id))
        self.assertIsNone(staff.by_id(beta.id))
        self.assertEqual(staff.by_id(runner.id), replace(runner, parent=None))
        self.assertEqual(staff.by_id(child.id), replace(child, parent=None))
        self.assertEqual(staff.snapshot().pending_retire, ())

    def test_permanent_wrap_rerun_reads_the_capture_after_unit_retirement(self) -> None:
        runner, alpha, beta, child, permanent = self.permanent_staff_tree()
        original = staff.retire
        def interrupted(staff_id: staff.StaffId) -> staff.StaffRow | None:
            row = original(staff_id)
            if staff_id == alpha.id:
                raise OSError("stopped after staff retirement")
            return row
        with mock.patch.object(staff, "retire", side_effect=interrupted):
            first = self.run_lifecycle("wrap", "--no-ci")
        self.assertEqual(first.returncode, 2, first.stdout + first.stderr)
        with mock.patch.object(showrunner_tree, "edges_to_release", side_effect=AssertionError("capture read twice")):
            result = self.run_lifecycle("wrap", "--no-ci")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(staff.by_id(permanent.id), permanent)
        self.assertEqual(staff.by_id(runner.id), replace(runner, parent=None))
        self.assertEqual(staff.by_id(child.id), replace(child, parent=None))
        self.assertIsNone(staff.by_id(beta.id))
        self.assertEqual(len(self.sent()), 1)
        before = self.staff.staff_file.read_bytes(), self.doc.read_bytes()
        self.assertEqual(self.run_lifecycle("wrap", "--no-ci").returncode, 0)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.doc.read_bytes()), before)
        self.assertEqual(len(self.sent()), 1)

    def test_wrap_release_retry_does_not_repeat_a_persisted_notice(self) -> None:
        self.assert_release_notice_retry(reported_failure=False)

    def test_wrap_release_keeps_failed_delivery_out_of_the_capture(self) -> None:
        self.assert_release_notice_retry(reported_failure=True)

    def test_wrap_uses_delivery_status_even_when_the_line_has_no_failure_prefix(self) -> None:
        _ = self.staff_tree()
        with (mock.patch.dict(os.environ, self.env),
              mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", self.state / "showrunner"),
              mock.patch.object(showrunners, "tell_others",
                                return_value=showrunners.NoticeDelivery(("delivery unavailable",), failed=("top",))),
              contextlib.redirect_stdout(io.StringIO())):
            _ = production_lifecycle.capture_release("example")
            with self.assertRaisesRegex(production_lifecycle.LifecycleStop, "delivery unavailable"):
                production_lifecycle.release_edges("example")
            self.assertEqual(production_lifecycle.capture_release("example").told, ())

    def assert_release_notice_retry(self, *, reported_failure: bool) -> None:
        _ = self.staff_tree()
        calls: list[str] = []
        def tell(texts: dict[str, str], key: str) -> showrunners.NoticeDelivery:
            target, = texts
            self.assertEqual(key, f"showrunner-tree:{'example' if target == 'top' else target}")
            calls.append(target)
            if target == "kid" and calls.count("kid") == 1:
                if reported_failure:
                    return showrunners.NoticeDelivery(("could not tell kid: unavailable",), failed=("kid",))
                raise OSError("second notice failed")
            return showrunners.NoticeDelivery((f"told {target}",))
        with mock.patch.dict(os.environ, self.env), mock.patch.object(
                showrunners, "SHOWRUNNER_STATE_DIR", self.state / "showrunner"), mock.patch.object(
                showrunners, "tell_others", side_effect=tell), contextlib.redirect_stdout(io.StringIO()):
            _ = production_lifecycle.capture_release("example")
            error = production_lifecycle.LifecycleStop if reported_failure else OSError
            reason = "could not tell kid: unavailable" if reported_failure else "second notice failed"
            with self.assertRaisesRegex(error, reason):
                production_lifecycle.release_edges("example")
            self.assertEqual(production_lifecycle.capture_release("example").told, ("top",))
            production_lifecycle.release_edges("example")
            production_lifecycle.release_edges("example")
        self.assertEqual(calls, ["top", "kid", "kid"])
        self.assertFalse((self.state / "showrunner/example/parent").exists())
        self.assertFalse((self.state / "showrunner/kid/parent").exists())

    def test_in_process_cli_matches_process_output_and_status(self) -> None:
        for arguments in (("load", "--no-ci", "--resume"), ("wrap", "--no-ci"),
                          ("promote-main", "--no-ci", "--ci-green", "abc")):
            with self.subTest(arguments=arguments):
                process = self.run_lifecycle_process(*arguments)
                direct = self.run_lifecycle(*arguments)
                self.assertEqual((direct.returncode, direct.stdout, direct.stderr),
                                 (process.returncode, process.stdout, process.stderr))

    def test_load_holds_checkout_on_other_branch(self) -> None:
        self.running()
        _ = self.git("switch", "main")
        result = self.run_lifecycle("load", "--no-ci", "--resume")
        self.assert_step(result, "held")
        self.assertIn("Run /showrunner:produce in a checkout on production.", result.stdout)

    def test_load_fails_when_production_doc_lacks_merge_branch(self) -> None:
        self.running()
        self.write(self.doc, self.doc.read_text().replace("- **Merge branch:** `production`\n", ""))
        result = self.run_lifecycle("load", "--no-ci", "--resume")
        self.assert_step(result, "failed")
        self.assertIn("Merge branch", result.stdout)

    def test_load_resume_reads_state_for_planned_production(self) -> None:
        common_dir = Path(self.git("rev-parse", "--git-common-dir"))
        with (self.checkout / common_dir / "info/exclude").open("a", encoding="utf-8") as output:
            _ = output.write("production.log\n")
        self.write(self.log, "# Production log — example\n\n### STATE\nresumed state\n")

        ordinary = self.run_lifecycle("load", "--no-ci")
        self.assert_step(ordinary, "ok")
        self.assertNotIn("resumed state", ordinary.stdout)
        resumed = self.run_lifecycle("load", "--no-ci", "--resume")
        self.assert_step(resumed, "ok")
        self.assertIn("resumed state", resumed.stdout)
        self.assertIn("alpha-unit: no code checkpoint", resumed.stdout)

    def test_load_copies_the_scratchpad_state_once_and_never_replaces_it(self) -> None:
        scratch = self.root / "scratch"
        self.write(scratch / "showrunner_state.json", '{"merges_held": ["old"]}\n')
        self.write(scratch / "unit_status" / "decisions_seen", "alpha-unit|1\n")
        target = self.state / "showrunner" / "example" / "session"

        first = self.run_lifecycle("load", "--no-ci", "--scratch", str(scratch))
        self.assert_step(first, "ok")
        self.assertIn("copied unit_status, showrunner_state.json", first.stdout)
        self.assertEqual((target / "unit_status" / "decisions_seen").read_text(), "alpha-unit|1\n")

        self.write(target / "showrunner_state.json", '{"merges_held": ["new"]}\n')
        again = self.run_lifecycle("load", "--no-ci", "--scratch", str(scratch))
        self.assert_step(again, "ok")
        self.assertNotIn("copied", again.stdout)
        self.assertIn('"new"', (target / "showrunner_state.json").read_text())

    def test_load_reports_each_last_code_merge_even_after_shrink(self) -> None:
        self.running()
        _, alpha_merge = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "2")
        _, beta_merge = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "2", shrink=True)
        _ = (self.state / "live-alpha").touch()
        result = self.run_lifecycle("load", "--no-ci", "--resume")
        self.assert_step(result, "ok")
        self.assertIn("old state", result.stdout)
        self.assertIn("alpha-unit", result.stdout)
        self.assertIn("beta-unit", result.stdout)
        self.assertIn(alpha_merge[:7], result.stdout)
        self.assertIn(beta_merge[:7], result.stdout)
        self.assertIn("session: ok — alpha-unit: gone", result.stdout)
        self.assertIn("session: ok — beta-unit: gone", result.stdout)

    def test_load_finds_each_unit_session_by_its_mark_whatever_it_is_called(self) -> None:
        self.running()
        fake_unit_host.write(self.env, [
            HostedUnit("alpha", "w1:p1", {}),
            HostedUnit("renamed-since-launch", "w2:p1", {
                "SHOWRUNNER_UNIT": "example", "SHOWRUNNER_UNIT_ID": "beta-unit"}),
        ])
        result = self.run_lifecycle("load", "--no-ci", "--resume")
        self.assert_step(result, "ok")
        self.assertIn("alpha-unit: gone", result.stdout)
        self.assertIn("beta-unit: renamed-since-launch live", result.stdout)

    def test_open_holds_when_production_is_wrapped(self) -> None:
        self.running()
        self.production_doc("wrapped")
        before = self.git("rev-parse", "HEAD")
        result = self.run_lifecycle("open", "--no-ci", "--session", "test-showrunner")
        self.assert_step(result, "held")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_open_resumes_at_push_after_rejection(self) -> None:
        reject = self.origin / "hooks/pre-receive"
        self.write(reject, "#!/bin/sh\nexit 1\n")
        reject.chmod(0o755)
        failed = self.run_lifecycle("open", "--no-ci", "--session", "test-showrunner")
        self.assert_step(failed, "failed")
        self.assertIn("push", failed.stdout.lower())
        first_tip = self.git("rev-parse", "HEAD")
        self.assertIn("running", self.doc.read_text())
        reject.unlink()
        retried = self.run_lifecycle("open", "--no-ci", "--session", "test-showrunner")
        self.assert_step(retried, "ok")
        self.assertEqual(self.git("rev-parse", "HEAD"), first_tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), first_tip)
        # The showrunner's name is written nowhere: open leaves no name in the doc.
        self.assertNotIn("test-showrunner", self.doc.read_text())
        self.assertTrue(self.log.read_text().startswith("# Production log — example"))
        common_dir = Path(self.git("rev-parse", "--git-common-dir"))
        self.assertIn("production.log", (self.checkout / common_dir / "info/exclude").read_text())

    def test_open_needs_no_session_name(self) -> None:
        result = self.run_lifecycle("open")
        self.assert_step(result, "ok")
        self.assertIn("doc: ok — running", result.stdout)

    def test_open_skips_missing_plan_then_commits_it_on_rerun(self) -> None:
        _ = self.git("rm", "--cached", "docs/plans/alpha.md")
        _ = self.git("rm", "docs/plans/beta.md")
        _ = self.git("commit", "-m", "prepare units before their plans")
        _ = self.git("switch", "-c", "production")
        alpha = (self.checkout / "docs/plans/alpha.md").read_text()

        first = self.run_lifecycle("open")

        self.assert_step(first, "ok")
        missing_reports = [line for line in first.stdout.splitlines() if "no plan yet" in line]
        self.assertEqual(len(missing_reports), 1, first.stdout)
        self.assertIn("beta-unit", missing_reports[0])
        self.assertIn("docs/plans/beta.md", missing_reports[0])
        self.assertFalse((self.checkout / "docs/plans/beta.md").exists())
        self.assertEqual(set(self.git("show", "--pretty=format:", "--name-only", "HEAD").splitlines()),
                         {"docs/plans/example-production.md", "docs/plans/alpha.md"})
        self.assertIn("**Status: PRODUCTION — running.**",
                      self.git("show", "HEAD:docs/plans/example-production.md"))
        self.assertEqual(self.git("show", "HEAD:docs/plans/alpha.md"), alpha.strip())
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(example): plans for 2 units")
        first_tip = self.git("rev-parse", "HEAD")
        beta = "# Beta plan\n\n## Phases\n\n1. Write the unit.\n"
        self.write(self.checkout / "docs/plans/beta.md", beta)
        self.write(self.checkout / "unrelated.txt", "leave this out\n")

        second = self.run_lifecycle("open")

        self.assert_step(second, "ok")
        self.assertNotIn("no plan yet", second.stdout)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), first_tip)
        self.assertEqual(self.git("show", "HEAD:docs/plans/beta.md"), beta.strip())
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"), "docs/plans/beta.md")
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(example): plans for 2 units")
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin),
                         self.git("rev-parse", "HEAD"))

    def test_open_rerun_does_not_commit_plans_twice_and_writes_no_session_name(self) -> None:
        first = self.run_lifecycle("open", "--session", "first-showrunner")
        self.assert_step(first, "ok")
        tip = self.git("rev-parse", "HEAD")
        second = self.run_lifecycle("open", "--session", "other-showrunner")
        self.assert_step(second, "ok")
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), tip)
        self.assertEqual(self.git("log", "--format=%s", "--grep=^production(example): plans for 2 units$", "production"),
                         "production(example): plans for 2 units")
        self.assertNotIn("first-showrunner", self.doc.read_text())
        self.assertNotIn("other-showrunner", self.doc.read_text())

    def test_open_running_with_committed_plans_skips_commit_and_push(self) -> None:
        self.running()
        tip = self.git("rev-parse", "HEAD")
        result = self.run_lifecycle("open", "--session", "other-showrunner")
        self.assert_step(result, "ok")
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), tip)
        self.assertIn("**Showrunner session:** old-showrunner", self.doc.read_text())

    def test_promote_main_holds_when_origin_main_is_ahead(self) -> None:
        self.running()
        self.write(self.checkout / "merge-only.txt", "merge\n")
        _ = self.git("add", "merge-only.txt")
        _ = self.git("commit", "-m", "merge work")
        _ = self.git("push", "origin", "production")
        writer = self.root / "main-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(writer), cwd=self.root)
        _ = self.git("config", "user.name", "Lifecycle Test", cwd=writer)
        _ = self.git("config", "user.email", "lifecycle@example.invalid", cwd=writer)
        self.write(writer / "main-only.txt", "main\n")
        _ = self.git("add", "main-only.txt", cwd=writer)
        _ = self.git("commit", "-m", "main moved", cwd=writer)
        _ = self.git("push", "origin", "main", cwd=writer)
        result = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(result, "held")
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin),
                         self.git("rev-parse", "main", cwd=writer))

    def test_promote_main_fails_when_origin_rejects_main_push(self) -> None:
        self.running()
        self.write(self.checkout / "merge-only.txt", "merge\n")
        _ = self.git("add", "merge-only.txt")
        _ = self.git("commit", "-m", "merge work")
        _ = self.git("push", "origin", "production")
        reject = self.origin / "hooks/pre-receive"
        self.write(reject, "#!/bin/sh\nexit 1\n")
        reject.chmod(0o755)
        result = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(result, "failed")
        self.assertIn("push", result.stdout.lower())

    def test_promote_main_requires_ci_and_smoke_evidence_on_same_tip(self) -> None:
        self.running()
        other = self.git("rev-parse", "HEAD")
        tip = self.merge_tip()
        main_before = self.git("rev-parse", "main", cwd=self.origin)

        no_ci_record = self.run_lifecycle("promote-main")
        self.assert_step(no_ci_record, "held")
        self.assertIn("ci: held", no_ci_record.stdout)
        green_without_smoke = self.run_lifecycle("promote-main", "--ci-green", tip)
        self.assert_step(green_without_smoke, "held")
        self.assertIn("smoke-launch: held", green_without_smoke.stdout)
        wrong_ci = self.run_lifecycle("promote-main", "--ci-green", other, "--smoke-passed", tip)
        self.assert_step(wrong_ci, "held")
        self.assertIn("ci: held", wrong_ci.stdout)
        self.assertIn(other, wrong_ci.stdout)
        self.assertIn(tip, wrong_ci.stdout)
        wrong_smoke = self.run_lifecycle("promote-main", "--ci-green", tip, "--smoke-passed", other)
        self.assert_step(wrong_smoke, "held")
        self.assertIn("smoke-launch: held", wrong_smoke.stdout)
        self.assertIn(other, wrong_smoke.stdout)
        self.assertIn(tip, wrong_smoke.stdout)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), main_before)
        self.assertEqual(self.git("rev-parse", "main"), main_before)

        incompatible = self.run_lifecycle("promote-main", "--no-ci", "--ci-green", tip)
        self.assert_step(incompatible, "failed")
        self.assertIn("input: failed — --no-ci excludes the verdict flags", incompatible.stdout)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), main_before)

        passed = self.run_lifecycle("promote-main", "--ci-green", tip, "--smoke-passed", tip[:9])
        self.assert_step(passed, "ok")
        self.assertIn("ci: ok", passed.stdout)
        self.assertIn("smoke-launch: ok", passed.stdout)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)

    def test_showrunner_instructions_explain_stop_resume_and_account_move(self) -> None:
        commands = Path(__file__).parents[2] / "commands/showrunner"
        for name in ("produce.md", "move.md"):
            with self.subTest(command=name):
                text = (commands / name).read_text(encoding="utf-8")
                for command in ("agents stop", "agents resume", "accounts.py move"):
                    self.assertIn(command, text)
                self.assertIn("--why", text)
                self.assertNotIn("<PauseForMove>", text)

    def test_produce_names_live_ci_and_smoke_steps(self) -> None:
        command = Path(__file__).parents[2] / "commands/showrunner/produce.md"
        text = command.read_text(encoding="utf-8")
        section = text.split("<PromoteMain>", 1)[-1].split("</PromoteMain>", 1)[0]
        for phrase in ("smoke_launch.sh", "gh run view", "--ci-green <sha>", "--smoke-passed <sha>"):
            self.assertIn(phrase, section)

    def test_promote_main_pushes_local_tip_when_only_origin_is_behind(self) -> None:
        self.running()
        _ = self.merge_tip()
        destination = self.promotion_checkout("installed")
        self.production_doc("running", promote=f"`{destination}`")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "declare promotion")
        _ = self.git("push", "origin", "production")
        tip = self.git("rev-parse", "HEAD")
        _ = self.git("fetch", "origin", "production", cwd=destination)
        _ = self.git("merge", "--ff-only", tip, cwd=destination)
        first = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(first, "ok")
        self.assertIn(f"{destination}: pushed {tip}", first.stdout)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)
        reject = self.origin / "hooks/pre-receive"
        self.write(reject, "#!/bin/sh\nexit 1\n")
        reject.chmod(0o755)
        second = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(second, "ok")
        self.assertIn(f"{destination}: already at {tip}", second.stdout)

    def test_promote_main_holds_foreign_local_main_commit_without_push(self) -> None:
        self.running()
        _ = self.merge_tip()
        destination = self.promotion_checkout("installed")
        self.production_doc("running", promote=f"`{destination}`")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "declare promotion")
        _ = self.git("push", "origin", "production")
        tip = self.git("rev-parse", "HEAD")
        _ = self.git("fetch", "origin", "production", cwd=destination)
        _ = self.git("merge", "--ff-only", tip, cwd=destination)
        self.write(destination / "foreign.txt", "foreign\n")
        _ = self.git("add", "foreign.txt", cwd=destination)
        _ = self.git("commit", "-m", "foreign main", cwd=destination)
        old_main = self.git("rev-parse", "main", cwd=self.origin)
        result = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(result, "held")
        self.assertIn("has main commits outside the merge branch", result.stdout)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), old_main)

    def test_promote_main_pulls_mac_with_and_without_ci(self) -> None:
        self.running()
        _ = self.merge_tip()
        destination = self.promotion_checkout("installed")
        mac = self.root / "mac-installed"
        self.production_doc("running", promote=f"`{destination}`, mac `{mac}`")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "declare promotion")
        _ = self.git("push", "origin", "production")
        tip = self.git("rev-parse", "HEAD")
        for options in (("--ci-green", tip, "--smoke-passed", tip), ("--no-ci",)):
            _ = (self.state / "calls").write_text("")
            result = self.run_lifecycle("promote-main", *options)
            self.assert_step(result, "ok")
            self.assertIn(f"mac {mac} main pulled", result.stdout)
            calls = [line for line in self.calls().splitlines() if line.startswith("ssh ")]
            self.assertEqual(len(calls), 1)
            self.assertIn("ssh mac", calls[0])
            self.assertIn(f"git pull --ff-only natedev:{destination} main", calls[0])
        _ = (self.state / "mac-rc").write_text("1\n")
        failed = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(failed, "failed")
        self.assertIn("promote: failed", failed.stdout)

    def test_promote_main_skips_ci_smoke_mac_and_repairs_behind_destination(self) -> None:
        self.running()
        self.write(self.checkout / "merge-only.txt", "merge\n")
        _ = self.git("add", "merge-only.txt")
        _ = self.git("commit", "-m", "merge work")
        _ = self.git("push", "origin", "production")
        tip = self.git("rev-parse", "HEAD")
        at_tip = self.root / "at-tip"
        behind = self.root / "behind"
        for path in (at_tip, behind):
            _ = self.git("clone", "-b", "main", str(self.origin), str(path), cwd=self.root)
            _ = self.git("config", "user.name", "Lifecycle Test", cwd=path)
            _ = self.git("config", "user.email", "lifecycle@example.invalid", cwd=path)
        _ = self.git("fetch", "origin", "production", cwd=at_tip)
        _ = self.git("merge", "--ff-only", tip, cwd=at_tip)
        self.production_doc("running", promote=f"`{at_tip}`, `{behind}`")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "set promotion destinations")
        _ = self.git("push", "origin", "production")
        tip = self.git("rev-parse", "HEAD")
        _ = self.git("fetch", "origin", "production", cwd=at_tip)
        _ = self.git("merge", "--ff-only", tip, cwd=at_tip)
        result = self.run_lifecycle("promote-main", "--no-ci")
        self.assert_step(result, "ok")
        self.assert_no_ci_skips(result.stdout)
        self.assertIn(f"{at_tip}: pushed {tip}", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=behind), tip)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)
        calls = (self.state / "calls").read_text() if (self.state / "calls").exists() else ""
        self.assertNotIn("ssh ", calls)
        self.assertNotIn("gh ", calls)

    def test_wrap_holds_unapproved_close_out(self) -> None:
        self.running()
        self.production_doc("running", close_out="- When done, delete the cache")
        before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "held")
        self.assertIn('When done, delete the cache is not recorded done', result.stdout)
        self.assertTrue(self.alpha.exists())
        self.assertIn("running", self.doc.read_text())
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)

        near = self.run_lifecycle("wrap", "--no-ci", "--close-out-done", "When done delete the cache")
        self.assert_step(near, "held")
        self.assertIn("When done, delete the cache", near.stdout)
        self.assertTrue(self.alpha.exists())

    def test_wrap_holds_planned_production(self) -> None:
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "held")
        self.assertIn("wrap: held", result.stdout)

    def test_wrap_held_at_close_out_keeps_its_style_child_attached_and_untold(self) -> None:
        self.running()
        self.production_doc("running", close_out="- Delete the cache")
        self.showrunner("kid", live=True)
        child_doc = self.root / "kid-production.md"
        self.write(child_doc, "# Production — kid\n\n- **Kind:** style\n")
        parent = self.reports_to("kid", "example")
        child = showrunners.Showrunner(
            slug="kid", session="kid-name", socket=str(self.root / "kid.sock"),
            zone="America/New_York", doc=str(child_doc),
        )
        production = read_production(self.doc)
        with (mock.patch.object(showrunner_tree, "stored_children", return_value=("kid",)),
              mock.patch.object(showrunners, "registered_showrunners", return_value=[child]),
              mock.patch.object(showrunner_parent, "detach") as detach,
              mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery()) as tell,
              contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaises(production_lifecycle.LifecycleStop) as stopped:
                _ = production_lifecycle.wrap(production, self.doc.read_text().splitlines(), True,
                                          production_lifecycle.VerdictMissing(),
                                          production_lifecycle.VerdictMissing(), ())
            detach.assert_not_called()
            tell.assert_not_called()
        self.assertEqual(stopped.exception.step, "close-out")
        self.assertEqual(stopped.exception.state, "held")
        self.assertEqual(parent.read_text(), "example\n")
        self.assertEqual(self.sent(), [])
        self.assertIn("**Status: PRODUCTION — running.**", self.doc.read_text())

    def test_wrap_rejects_verdict_for_other_tip_before_retirement(self) -> None:
        self.running()
        other = self.git("rev-parse", "HEAD")
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        tip = self.git("rev-parse", "HEAD")
        origin_before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        result = self.run_lifecycle("wrap", "--ci-green", other, "--smoke-passed", tip)
        self.assert_step(result, "held")
        self.assertIn("ci: held", result.stdout)
        self.assertTrue(self.alpha.exists())
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), origin_before)

        incompatible = self.run_lifecycle("wrap", "--no-ci", "--smoke-passed", tip)
        self.assert_step(incompatible, "failed")
        self.assertIn("input: failed — --no-ci excludes the verdict flags", incompatible.stdout)

    def test_wrap_accepts_exact_close_out_completion(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        item = "Archive records to safe location."
        self.production_doc("running", close_out=f"- {item}")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "record close-out item")
        _ = self.git("push", "origin", "production")

        result = self.run_lifecycle("wrap", "--no-ci", "--close-out-done", item)
        self.assert_step(result, "ok")
        self.assertIn(f"close-out: ok — {item} (recorded done)", result.stdout)
        self.assertIn("wrapped", self.doc.read_text())

    def test_wrap_requires_merged_remote_branch_when_worktree_is_gone(self) -> None:
        self.running()
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        self.write(self.alpha / "unmerged.txt", "unmerged\n")
        _ = self.git("add", "unmerged.txt", cwd=self.alpha)
        _ = self.git("commit", "-m", "unmerged", cwd=self.alpha)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.alpha)
        _ = self.git("worktree", "remove", str(self.alpha))
        _ = self.git("branch", "-D", "alpha-branch")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "held")
        self.assertIn("alpha-branch", result.stdout)
        self.assertEqual(self.git("ls-remote", "--heads", "origin", "alpha-branch").split()[1],
                         "refs/heads/alpha-branch")

    def test_wrap_reports_already_retired_unit_with_no_branches(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        _ = self.git("worktree", "remove", str(self.alpha))
        _ = self.git("branch", "-d", "alpha-branch")
        _ = self.git("push", "origin", "--delete", "alpha-branch")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertIn("alpha-unit", result.stdout)
        self.assertIn("already retired", result.stdout)

    def test_wrap_releases_reservation_from_unit_worktree_through_python_wrapper(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        alpha = {"reservation_id": "alpha-reservation", "target": "refs/heads/production",
                 "holder": {"worktree_root": str(self.alpha), "branch": "alpha-branch"},
                 "scopes": ["file:alpha.txt"], "lifecycle": "active", "edit_blocking_status": "blocking"}
        unrelated = {**alpha, "reservation_id": "other-reservation",
                     "holder": {"worktree_root": str(self.root / "other"), "branch": "other"}}
        empty: dict[str, object] = {"journal_position": {}, "entries": []}
        board: dict[str, object] = {"output_contract_version": 1, "verb": "board", "status": "ok",
                 "reservations": ["alpha-reservation", "other-reservation"],
                 "payload": {"kind": "board", "data": {
                     "targets": [{"reservations": [alpha, unrelated]}],
                     "ready_now": {"journal_position": {}, "entries": [alpha]},
                     "waiting": empty, "unresolved_overlaps": empty,
                     "unconstrained_reservations": empty, "resolved": empty}}}
        self.write(self.state / "reservations.json", json.dumps(board))
        self.write(self.state / "board-exit", "0")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertIn(f"cargo-berth release alpha-reservation --json cwd={self.alpha}", self.calls())
        self.assertEqual(self.calls().count("cargo-berth release alpha-reservation"), 1)
        self.assertNotIn("cargo-berth release other-reservation", self.calls())
        self.assertIn(f"cargo-berth board --json cwd={self.alpha}", self.calls())
        self.assertNotIn("retire: skipped", result.stdout)
        # Removing the update timer is the whole of forgetting a showrunner: no registry is rewritten.
        self.assertNotIn("showrunners.py", self.calls())

    def test_wrap_reports_an_unavailable_board_and_releases_nothing(self) -> None:
        self.merged_units()
        self.write(self.state / "reservations.json", json.dumps({
            "output_contract_version": 1, "verb": "board", "status": "ledger_unreadable",
            "message": "journal is unreadable", "payload": {"kind": "no_facts"}}))
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertIn("retire: skipped — board unavailable (journal is unreadable)", result.stdout)
        self.assertNotIn("cargo-berth release", self.calls())

    def test_wrap_reports_an_empty_board_and_releases_nothing(self) -> None:
        self.merged_units()
        self.write(self.state / "reservations.json", "")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertIn("retire: skipped — board unavailable (cargo-berth returned no board (exit 4))", result.stdout)
        self.assertNotIn("cargo-berth release", self.calls())

    def test_wrap_reports_a_malformed_board_and_releases_nothing(self) -> None:
        self.merged_units()
        self.write(self.state / "reservations.json", "{not-json")
        self.write(self.state / "board-exit", "0")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertIn("retire: skipped — board unavailable (", result.stdout)
        self.assertNotIn("cargo-berth release", self.calls())

    def test_wrap_an_unconfigured_repository_releases_nothing_without_a_skip(self) -> None:
        self.merged_units()
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertIn("cargo-berth board --json", self.calls())
        self.assertNotIn("cargo-berth release", self.calls())
        self.assertNotIn("retire: skipped", result.stdout)

    def test_wrap_resumes_after_final_push_rejection_without_second_commit(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        marker = self.root / "reject-production"
        marker.touch()
        hook = self.origin / "hooks/pre-receive"
        self.write(hook, '#!/bin/sh\nwhile read old new ref; do\n'
                   + f'  if [ "$ref" = "refs/heads/production" ] && [ -e "{marker}" ]; then exit 1; fi\n'
                   + 'done\nexit 0\n')
        hook.chmod(0o755)
        failed = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(failed, "failed")
        self.assertIn("wrapped", self.doc.read_text())
        wrapped_tip = self.git("rev-parse", "HEAD")
        self.assertNotEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), wrapped_tip)
        self.assertFalse(self.alpha.exists())
        retired_calls = self.calls()
        marker.unlink()
        retried = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(retried, "ok")
        self.assertIn("| Next |", retried.stdout)
        self.assertIn("by the earlier run |", retried.stdout)
        self.assertNotIn("(0 commits)", retried.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), wrapped_tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), wrapped_tip)
        self.assertEqual(self.git("log", "--format=%s", "--grep=^production(example): wrapped$", "production"),
                         "production(example): wrapped")
        self.assertEqual(self.calls(), retired_calls)
        calls_after = self.calls()
        already_pushed = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(already_pushed, "ok")
        self.assertIn("| Next |", already_pushed.stdout)
        self.assertEqual(self.calls(), calls_after)

    def test_wrap_fails_on_dirty_unit_worktree(self) -> None:
        self.running()
        self.write(self.alpha / "dirty.txt", "unsaved\n")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "failed")
        self.assertIn("alpha", result.stdout)
        self.assertTrue(self.alpha.exists())

    def test_wrap_without_ci_retires_units_removes_notifier_and_reports(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assert_no_ci_skips(result.stdout)
        self.assertFalse(self.alpha.exists())
        self.assertFalse(self.beta.exists())
        self.assertIn("wrapped", self.doc.read_text())
        for area in ("Units", "Merge branch", "CI", "Close-out", "Main", "Next"):
            self.assertIn(f"| {area} |", result.stdout)
        calls = (self.state / "calls").read_text()
        self.assertIn("notifier.sh remove", calls)
        self.assertNotIn("showrunners.py", calls)
        self.assertNotIn("gh ", calls)
        self.assertNotIn("ssh ", calls)
        self.assertIn("tree: ok — no parent and no child showrunners", result.stdout)
        self.assertEqual(self.sent(), [])

    def test_wrap_moves_a_doc_out_of_productions_into_the_archive(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        kept = self.checkout / "productions/example-production.md"
        kept.parent.mkdir()
        _ = self.git("mv", str(self.doc), str(kept))
        _ = self.git("commit", "-m", "keep the doc in productions/")
        _ = self.git("push", "origin", "production")
        self.doc = kept

        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        relative = "archive/checkout/example/example-production.md"
        archived = self.checkout / relative
        self.assertIn(f"wrap: ok — moved the doc out of productions/ to {relative}", result.stdout)
        self.assertFalse(kept.exists())
        self.assertIn("wrapped", archived.read_text())
        self.assertEqual(self.git("status", "--short"), "")
        moved = self.git("show", "--name-status", "--format=%s", "HEAD")
        self.assertIn("production(example): wrapped", moved)
        self.assertIn(relative, moved)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "origin/production"))

    def test_same_wrap_command_finishes_after_the_doc_was_moved(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        kept = self.checkout / "productions/example-production.md"
        kept.parent.mkdir()
        _ = self.git("mv", str(self.doc), str(kept))
        _ = self.git("commit", "-m", "keep the doc in productions/")
        _ = self.git("push", "origin", "production")
        self.doc = kept
        real_checked = production_lifecycle.checked

        def stop_at_commit(checkout: Path, step: str, *arguments: str) -> str:
            if step == "wrap" and arguments[:1] == ("add",):
                raise OSError("stopped after mv")
            return real_checked(checkout, step, *arguments)

        with mock.patch.object(production_lifecycle, "checked", stop_at_commit):
            stopped = self.run_lifecycle("wrap", "--no-ci")
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertFalse(kept.exists())
        finished = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(finished, "ok")
        self.assertIn("| Next | close herdr sessions |", finished.stdout)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "origin/production"))
        self.assertIn("wrapped", (self.checkout / "archive/checkout/example/example-production.md").read_text())

    def test_wrap_names_herdr_sessions_for_close_out(self) -> None:
        self.merged_units()
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertIn("| Next | close herdr sessions |", result.stdout)

    def test_wrap_detaches_from_its_parent_and_leaves_each_child_a_root(self) -> None:
        self.merged_units()
        for slug in ("top", "kid", "other"):
            self.showrunner(slug, live=slug == "top")
        own = self.reports_to("example", "top")
        kid = self.reports_to("kid", "example")
        other = self.reports_to("other", "top")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertFalse(own.exists())
        self.assertFalse(kid.exists())
        self.assertEqual(other.read_text(encoding="utf-8"), "top\n")
        self.assertRegex(result.stdout, r"(?m)^tree: ok — detached from top; kid now root; told top; "
                         + r"kid is not running: tell it when it starts$")
        [sent] = self.sent()
        self.assertEqual(sent[sent.index("--to") + 1], f"uds:{self.root / 'top.sock'}")
        self.assertEqual(sent[sent.index("--text") + 1], "example wrapped, so it no longer reports to you.")
        self.assertIn("wrapped", self.doc.read_text())

    def test_wrap_deletes_its_own_state_files_and_keeps_other_productions(self) -> None:
        self.merged_units()
        state = self.state / "showrunner"
        own = (state / "footers-off/example", state / "review-paused/example.json",
               state / "outstanding/example.json", state / "example/prompt.txt")
        other = (state / "footers-off/other", state / "outstanding/other.json", state / "other/prompt.txt")
        for path in (*own, *other):
            self.write(path, "[]\n")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "ok")
        self.assertRegex(result.stdout, r"(?m)^tidy: ok — removed footers-off/example, review-paused/example.json, "
                         + r"outstanding/example.json, example/$")
        self.assertFalse(any(path.exists() for path in own))
        self.assertFalse((state / "example").exists())
        self.assertTrue(all(path.exists() for path in other))

    def test_wrap_holds_while_a_child_showrunner_runs(self) -> None:
        self.merged_units()
        self.showrunner("kid", live=True)
        self.showrunner("idle")
        kid = self.reports_to("kid", "example")
        idle = self.reports_to("idle", "example")
        result = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(result, "held")
        self.assertRegex(result.stdout, r"(?m)^children: held — kid still reports to example; wrap each first")
        self.assertNotIn("idle", result.stdout)
        self.assertTrue(kid.exists())
        self.assertTrue(idle.exists())
        self.assertTrue(self.alpha.exists())
        self.assertNotIn("notifier.sh", self.calls())
        self.assertIn("**Status: PRODUCTION — running.**", self.doc.read_text())

        self.write(self.root / "kid-production.md", "# Kid\n\n- **Kind:** product\n")
        explicit_product = self.run_lifecycle("wrap", "--no-ci")
        self.assert_step(explicit_product, "held")
        self.assertIn("children: held — kid still reports to example", explicit_product.stdout)
        self.assertTrue(kid.exists())
        self.assertEqual(self.sent(), [])

    def test_wrap_detaches_and_tells_a_style_child_without_retiring_its_work(self) -> None:
        self.merged_units()
        style = self.root / "checkout-style"
        unit = self.root / "checkout-style-names"
        _ = self.git("worktree", "add", "-b", "style/main", str(style), "main")
        _ = self.git("worktree", "add", "-b", "style-names", str(unit), "style/main")
        style_doc = style / "docs/kid-production.md"
        self.write(style_doc, "# Production — kid\n\n- **Kind:** style\n"
                   + f"- **Showrunner checkout:** `{style}`\n\n## Units\n\n"
                   + "| Unit | Plan | Worktree | Branch | Port | Owns |\n"
                   + "| --- | --- | --- | --- | --- | --- |\n"
                   + f"| names-unit | docs/style-production/names.md | {unit} | style-names | — | none |\n")
        plan = unit / "docs/style-production/names.md"
        pinned = unit / "docs/style-production/rules/names.md"
        ledger = self.state / "style/ledger.sqlite3"
        self.write(plan, "# Unit's own standing plan\n\n## Phases\n\n- active ticket\n")
        self.write(pinned, "# Pinned rule\n")
        self.write(ledger, "saved ledger and pending hand-offs\n")
        self.showrunner("kid", live=True)
        timer = self.state / "notifier/showrunner-kid/conf"
        self.write(timer, f"TARGET=session:kid-session\nCHECK=zsh /x/production_check.sh {style_doc}\n")
        parent = self.reports_to("kid", "example")
        child_state = self.state / "showrunner/kid/prompt.txt"
        self.write(child_state, "standing work remains\n")
        preserved = {path: path.read_bytes() for path in (style_doc, plan, pinned, ledger, timer, child_state)}
        style_tip = self.git("rev-parse", "HEAD", cwd=style)
        unit_tip = self.git("rev-parse", "HEAD", cwd=unit)

        result = self.run_lifecycle("wrap", "--no-ci")

        self.assert_step(result, "ok")
        self.assertFalse(parent.exists())
        child = next(row for row in staff.find(kind="showrunner") if row.name == "kid")
        self.assertIsNone(child.parent)
        self.assertIn("children: ok — detached kid; now root; told kid", result.stdout)
        [sent] = self.sent()
        self.assertEqual(sent[sent.index("--to") + 1], f"uds:{self.root / 'kid.sock'}")
        self.assertEqual(sent[sent.index("--text") + 1],
                         "example wrapped, so you are a root now: run style_sync.py resume and merge your own hand-offs "
                         + "(docs/style_production.md, Merging back).")
        for path, content in preserved.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=style), style_tip)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=unit), unit_tip)
        self.assertEqual(self.git("branch", "--show-current", cwd=style), "style/main")
        self.assertEqual(self.git("branch", "--show-current", cwd=unit), "style-names")
        self.assertIn("**Status: PRODUCTION — wrapped.**", self.doc.read_text())

    def test_wrap_archives_owned_phase_note_and_keeps_other_files(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        owned = self.write_phase_note("alpha", "example", "alpha-unit")
        owned_bytes = owned.read_bytes()
        other = self.write_phase_note("other", "other", "alpha-unit")
        other_bytes = other.read_bytes()
        handwritten = self.phase_table_vault / "showrunner" / "notes.md"
        self.write(handwritten, "kept by a person\n")

        result = self.run_lifecycle("wrap", "--no-ci")

        archived = (
            self.phase_table_vault.parent
            / "archive"
            / "showrunner"
            / "example"
            / "alpha.md"
        )
        self.assert_step(result, "ok")
        self.assertIn("phase-notes: ok — archived 1 phase note", result.stdout)
        self.assertFalse(owned.exists())
        self.assertEqual(archived.read_bytes(), owned_bytes)
        self.assertEqual(other.read_bytes(), other_bytes)
        self.assertEqual(handwritten.read_text(encoding="utf-8"), "kept by a person\n")

    def test_wrap_stays_running_when_phase_note_archive_fails(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha-unit", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta-unit", "1")
        _ = self.git("push", "origin", "production")
        note = self.write_phase_note("alpha", "example", "alpha-unit")
        archive = self.phase_table_vault.parent / "archive"
        self.write(archive, "blocks the archive directory\n")

        result = self.run_lifecycle("wrap", "--no-ci")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^phase-notes: failed — .+")
        self.assertIn("cannot archive phase note", result.stdout)
        self.assertTrue(note.is_file())
        self.assertIn("**Status: PRODUCTION — running.**", self.doc.read_text())
        self.assertIn("notifier.sh remove", self.calls())


if __name__ == "__main__":
    _ = unittest.main()


class PermanentUnitTests(unittest.TestCase):
    def test_wrap_neither_waits_on_nor_retires_a_permanent_unit(self) -> None:
        sys.path.insert(0, str(SCRIPT.parent))
        import production_lifecycle
        from production_units import Production
        from zoneinfo import ZoneInfo

        lines = ["## Units", "", "| Unit | Plan | Worktree | Branch | Port | Owns |", "| --- | --- | --- | --- | --- | --- |",
                 "| alpha-unit | docs/plans/alpha.md | /w/alpha | demo-alpha | — | — |",
                 "| nightly-review-unit | permanent | /w/nightly | demo-nightly-review | — | — |", ""]
        production = Production(Path("/c/docs/demo-production.md"), "demo", "demo", Path("/c"), Path("/c/log.md"), ZoneInfo("UTC"))
        self.assertEqual([row.name for row in production_lifecycle.units(lines, production)], ["alpha-unit"])
