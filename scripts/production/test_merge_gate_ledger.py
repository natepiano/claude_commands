"""Merge gate ledger contracts."""

from __future__ import annotations

import unittest

from datetime import UTC, datetime
import json
import io
import os
from pathlib import Path
from typing import cast, final
from contextlib import redirect_stdout
from unittest import mock
from zoneinfo import ZoneInfo
from scripts.production import merge_gate
from scripts.production.affected_modules import CertifiedBase, FullSuite, NoCertifiedBase
from scripts.buildlog.store import host_name
from scripts.production.merge_gate_test_support import FIXTURE_FOLDERS, MergeGateFixture, SUITE_MODULE, gate_suites


@final
class MergeGateLedgerTests(MergeGateFixture):
    @gate_suites(*FIXTURE_FOLDERS)
    def test_clean_pass_records_tree_commit_host_time_checkout_suites_and_source(self) -> None:
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_all_modules_started()
        record, = self.pass_records()
        self.assertEqual(record["tree"], self.git(self.checkout, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(record["commit"], self.base)
        self.assertEqual(record["host"], host_name())
        self.assertEqual(record["checkout"], str(self.checkout))
        self.assertEqual(record["suites"], sorted(self.suites))
        self.assertEqual(record["source"], "tests-only")
        at = datetime.fromisoformat(cast(str, record["at"]))
        self.assertEqual(at.utcoffset(), UTC.utcoffset(at))

    def test_dirty_and_failed_runs_do_not_record_a_pass(self) -> None:
        for state in ("tracked", "staged", "untracked", "failed"):
            with self.subTest(state=state):
                self.environment["MERGE_GATE_PASSED_DIR"] = str(self.state / state)
                self.clear_gate_events()
                if state == "tracked":
                    self.write(self.checkout / "subject.txt", "edited\n")
                elif state == "staged":
                    _ = self.git(self.checkout, "add", "subject.txt")
                elif state == "untracked":
                    _ = self.git(self.checkout, "restore", "--staged", "--worktree", "subject.txt")
                    self.write(self.checkout / "notes", "untracked\n")
                else:
                    (self.checkout / "notes").unlink()
                    self.write(self.state / "red-hooks", "red")
                result = self.run_gate("--tests-only")
                self.assertEqual(result.returncode, 1 if state == "failed" else 0, result.stdout + result.stderr)
                self.assertEqual(self.pass_records(), [])
                self.assertTrue(self.module_events())

    def test_second_tests_only_run_reuses_without_starting_any_module(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_gate_events()
        self.environment["USER_ZONE"] = "America/New_York"
        second = self.run_gate("--tests-only")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.module_events(), [])
        record, = self.pass_records()
        when = datetime.fromisoformat(cast(str, record["at"])).astimezone(
            ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")
        self.assertIn(f"tests reused from {cast(str, record['tree'])[:12]} ({when}, tests-only, {self.checkout})", second.stdout)

    def test_a_changed_suite_set_cannot_reuse_or_certify_an_old_full_pass(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        original, = self.pass_records()
        self.write(self.checkout / "scripts" / "added_folder" / "test_added.py", SUITE_MODULE)
        _ = self.commit(self.checkout, "new test folder")
        tree = merge_gate.clean_test_tree(self.checkout)
        folders = sorted([*self.suites, "added_folder"])
        ledger = merge_gate.PassedTreeLedger(self.checkout, "tests-only", tree, folders)
        with mock.patch.dict(os.environ, self.environment):
            # The old full pass cannot certify an ancestor after a folder is added.
            self.assertIsInstance(ledger.certified_base(), NoCertifiedBase)
            # A pass from the old gate could have this same tree but omit its new folder.
            original["tree"] = self.git(self.checkout, "rev-parse", "HEAD^{tree}")
            self.write(ledger.path, json.dumps(original) + "\n")
            self.assertFalse(ledger.reuse())
            self.assertIsInstance(ledger.certified_base(), NoCertifiedBase)
        self.clear_gate_events()
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("tests reused from", result.stdout)
        self.assertIn("full suite: no certified base", result.stdout)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual(starts, [("hooks", "test_first.py"), ("added_folder", "test_added.py")])
        self.assertEqual(self.pass_records()[-1]["suites"], folders)

    def test_checkpoint_reuses_unit_tree_and_still_runs_types_and_checks_settings(self) -> None:
        first = self.run_gate("--tests-only", "--repo", str(self.unit))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_gate_events()
        second = self.run_gate()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.module_events(), [])
        self.assertTrue((self.state / "types-ran").exists())
        self.assertIn("tests reused from", second.stdout)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), self.git(self.checkout, "rev-parse", "HEAD"))
        self.clear_gate_events()
        refused = self.run_gate("--settings-blob", "00000000")
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("tests reused from", refused.stdout)
        self.assertIn("settings.json differs", refused.stderr)
        self.assertEqual(self.module_events(), [])

    @gate_suites("lib", "hooks")
    def test_checkpoint_full_pass_records_and_tests_only_reuses_it(self) -> None:
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assert_all_modules_started()
        record, = self.pass_records()
        self.assertEqual(record["source"], "checkpoint")
        self.assertEqual(record["commit"], self.git(self.checkout, "rev-parse", "HEAD"))
        self.clear_gate_events()
        second = self.run_gate("--tests-only")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.module_events(), [])
        self.assertIn(", checkpoint, ", second.stdout)

    @gate_suites("lib", "hooks")
    def test_dirty_host_or_suite_mismatch_runs_all_modules(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        original = self.pass_records()[0]
        path = Path(self.environment["MERGE_GATE_PASSED_DIR"]) / "passed-trees.jsonl"
        for mismatch in ("host", "suites", "dirty"):
            with self.subTest(mismatch=mismatch):
                record = dict(original)
                if mismatch == "host":
                    record["host"] = "other-host"
                elif mismatch == "suites":
                    record["suites"] = [*self.suites, "other-suite"]
                else:
                    self.write(self.checkout / "subject.txt", "new tree\n")
                self.write(path, json.dumps(record) + "\n")
                self.clear_gate_events()
                result = self.run_gate("--tests-only")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("tests reused from", result.stdout)
                self.assert_all_modules_started()

    @gate_suites("lib", "hooks", "message")
    def test_affected_modules_are_reported_run_and_recorded_without_certifying_a_full_pass(self) -> None:
        subject = self.checkout / "scripts/hooks/affected_subject.py"
        self.write(subject, "VALUE = 1\n")
        test = self.checkout / "scripts/hooks/test_first.py"
        self.write(test, "import affected_subject\n" + SUITE_MODULE)
        base = self.commit(self.checkout, "dependency fixture")
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.write(subject, "VALUE = 2\n")
        _ = self.commit(self.checkout, "subject changed")
        for _attempt in range(2):
            self.clear_gate_events()
            result = self.run_gate("--tests-only")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
            self.assertEqual(starts, [("hooks", "test_first.py")])
            self.assertIn(f"affected: 1 of {self.module_count} modules (base {base[:12]}, ", result.stdout)
            self.assertIn("), 1 changed files\nhooks/test_first.py\n", result.stdout)
            self.assertIn("message: skipped (not affected)", result.stdout)
            self.assertIn("Tree-scanning modules: 0 (always affected)", result.stdout)
            self.assertNotIn("tests reused from", result.stdout)
        self.assertEqual([record["kind"] for record in self.pass_records()], ["full", "affected", "affected"])

    def test_unrelated_change_can_pass_with_zero_selected_modules(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.write(self.checkout / "subject.txt", "unrelated document\n")
        _ = self.commit(self.checkout, "documentation changed")
        self.clear_gate_events()
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"affected: 0 of {self.module_count} modules", result.stdout)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.pass_records()[-1]["kind"], "affected")

    @gate_suites("lib", "hooks")
    def test_full_flag_and_red_marker_force_all_modules_and_green_clears_the_marker(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        marker = Path(self.environment["MERGE_GATE_STATE_DIR"]) / "full-required"
        for trigger in ("--full", "red marker"):
            with self.subTest(trigger=trigger):
                if trigger == "red marker":
                    self.write(marker, json.dumps({"tree": "red-tree", "at": "2026-10-29T00:00:00+00:00"}))
                self.clear_gate_events()
                result = self.run_gate("--tests-only", *(["--full"] if trigger == "--full" else []))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_all_modules_started()
                self.assertNotIn("tests reused from", result.stdout)
                reason = ("--full" if trigger == "--full" else
                          "last nightly run was red (red-tree, 2026-10-29T00:00:00+00:00)")
                self.assertIn("full suite: " + reason, result.stdout)
                summary = next(line for line in result.stdout.splitlines() if line.startswith("Gate tests:"))
                self.assertIn("full suite: " + reason, summary)
                self.assertEqual(self.pass_records()[-1]["reason"], reason)
                self.assertFalse(marker.exists())
                self.assertEqual(self.pass_records()[-1]["kind"], "full")

    @gate_suites("lib", "hooks")
    def test_base_search_uses_only_newest_full_same_host_suite_ancestor_and_accepts_legacy_records(self) -> None:
        ledger = merge_gate.PassedTreeLedger(self.checkout, "tests-only", merge_gate.clean_test_tree(self.checkout),
                                            sorted(self.suites))
        with mock.patch.dict(os.environ, self.environment):
            ledger.record_pass(FullSuite("no certified base"))
            original = self.pass_records()[0]
            _ = original.pop("kind")  # Older full certifications remain usable.
            _ = original.pop("reason")
            self.write(self.checkout / "subject.txt", "a descendant\n")
            head = self.commit(self.checkout, "descendant")
            detached = self.git(self.checkout, "commit-tree", self.git(self.checkout, "rev-parse", "HEAD^{tree}"),
                                "-m", "not an ancestor")
            records = [original]
            for index, changes in enumerate((
                {"kind": "affected", "commit": head}, {"host": "another-host", "commit": head},
                {"suites": ["hooks"], "commit": head}, {"commit": detached},
            )):
                records.append({**original, "at": f"2099-01-0{index + 1}T00:00:00+00:00", **changes})
            self.write(ledger.path, "".join(json.dumps(record) + "\n" for record in records))
            selected = ledger.certified_base()
            self.assertIsInstance(selected, CertifiedBase)
            assert isinstance(selected, CertifiedBase)
            self.assertEqual(selected.commit, self.base)
            # A newer qualifying certification wins even if appended earlier.
            records.insert(0, {**original, "commit": head, "at": "2099-02-01T00:00:00+00:00"})
            self.write(ledger.path, "".join(json.dumps(record) + "\n" for record in records))
            selected = ledger.certified_base()
            self.assertIsInstance(selected, CertifiedBase)
            assert isinstance(selected, CertifiedBase)
            self.assertEqual(selected.commit, head)

    def test_nightly_uses_a_disposable_shared_clone_and_marks_and_notifies_only_red(self) -> None:
        _ = self.git(self.unit, "push", "origin", "HEAD:integration")
        self.write(self.checkout / "untracked.txt", "keep the caller's edits\n")
        before_head = self.git(self.checkout, "rev-parse", "HEAD")
        before_status = self.git(self.checkout, "status", "--porcelain")
        before_branches = self.git(self.checkout, "show-ref", "--heads")
        copies: list[Path] = []
        marker = Path(self.environment["MERGE_GATE_STATE_DIR"]) / "full-required"
        for red in (True, False):
            with self.subTest(red=red):
                def suite_run(repo: Path, memory_cap_mib: int, type_dirs: list[str], *, module_time_limit_s: float,
                              full: bool) -> merge_gate.GateRun:
                    self.assertNotEqual(repo, self.checkout)
                    self.assertTrue((repo / ".git/objects/info/alternates").is_file())
                    self.assertEqual(merge_gate.checked_git(repo, "rev-parse", "HEAD"), self.checkpoint)
                    self.assertEqual(merge_gate.checked_git(repo, "status", "--porcelain"), "")
                    self.assertTrue(full)
                    self.assertGreater(memory_cap_mib, 0)
                    self.assertGreater(module_time_limit_s, 0)
                    self.assertEqual(type_dirs, ["scripts"])
                    self.write(repo / "subject.txt", "only the copy changes\n")
                    copies.append(repo)
                    return merge_gate.GateRun((merge_gate.GateJobResult("hooks", "test_first.py", int(red), 0, "fixture"),), 0, 0)

                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        mock.patch.object(merge_gate, "run_suites", side_effect=suite_run), \
                        mock.patch.object(merge_gate, "tell_nightly_red") as notice, redirect_stdout(output):
                    code = merge_gate.main(["--nightly", "--repo", str(self.checkout), "--merge-branch", "integration"])
                self.assertEqual(code, int(red), output.getvalue())
                self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), before_head)
                self.assertEqual(self.git(self.checkout, "status", "--porcelain"), before_status)
                self.assertEqual(self.git(self.checkout, "show-ref", "--heads"), before_branches)
                self.assertEqual((self.checkout / "subject.txt").read_text(), "base\n")
                self.assertTrue(all(not path.exists() for path in copies))
                if red:
                    record = cast(dict[str, object], json.loads(marker.read_text()))
                    self.assertEqual(record["tree"], self.git(self.checkout, "rev-parse", f"{self.checkpoint}^{{tree}}"))
                    self.assertEqual(record["failed_modules"], ["hooks/test_first.py"])
                    notice.assert_called_once()
                    self.assertEqual(notice.call_args.args[2], ["hooks/test_first.py"])
                else:
                    self.assertFalse(marker.exists())
                    notice.assert_not_called()

    def test_nightly_notice_resolves_the_production_from_its_branch_without_a_calling_session(self) -> None:
        from scripts.production import showrunners

        self.write(self.quiet_production, "- **Merge branch:** `integration`\n")
        output = io.StringIO()
        with mock.patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": ""}), \
                mock.patch.object(showrunners, "timer_docs", return_value={"quiet": self.quiet_production}), \
                mock.patch.object(showrunners, "tell", return_value="could not tell quiet: offline") as send, \
                redirect_stdout(output):
            merge_gate.tell_nightly_red(merge_gate.GateArguments(merge_branch="integration"),
                                        "fixture-tree", ["hooks/test_first.py"])
        send.assert_called_once()
        arguments = send.call_args
        assert arguments is not None
        self.assertEqual(arguments.args[:2], ("quiet", "merge-gate-nightly:fixture-tree"))
        self.assertIn("hooks/test_first.py", cast(str, arguments.args[2]))
        self.assertIn("could not tell quiet: offline", output.getvalue())

    def test_unavailable_ledger_runs_all_modules_and_reports_once(self) -> None:
        directory = Path(self.environment["MERGE_GATE_PASSED_DIR"])
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        path = directory / "passed-trees.jsonl"
        saved = path.read_text()
        for failure in ("unreadable", "unwritable", "invalid"):
            with self.subTest(failure=failure):
                self.write(path, "invalid json\n" if failure == "invalid" else saved)
                path.chmod(0o200 if failure == "unreadable" else 0o400 if failure == "unwritable" else 0o600)
                self.clear_gate_events()
                try:
                    result = self.run_gate("--tests-only")
                finally:
                    path.chmod(0o600)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.count("Test pass ledger unavailable;"), 1, result.stdout)
                self.assert_all_modules_started()

    def test_changed_checkout_or_failed_append_cannot_record_a_pass(self) -> None:
        finish = merge_gate.finish_gate_job
        for state in ("dirty-at-end", "dirty-at-start", "new-head", "append-fails"):
            with self.subTest(state=state):
                self.environment["MERGE_GATE_PASSED_DIR"] = str(self.state / state)
                self.clear_gate_events()
                if state == "dirty-at-start":
                    self.write(self.checkout / "subject.txt", "temporary edit\n")

                def finish_and_change(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
                    result = finish(job, status)
                    if state == "dirty-at-end":
                        self.write(self.checkout / "subject.txt", "edit during run\n")
                    elif state == "dirty-at-start":
                        _ = self.git(self.checkout, "restore", "subject.txt")
                    elif state == "new-head":
                        self.write(self.checkout / "subject.txt", "new commit during run\n")
                        _ = self.commit(self.checkout, "changed during tests")
                    else:
                        path = Path(self.environment["MERGE_GATE_PASSED_DIR"]) / "passed-trees.jsonl"
                        path.unlink()
                        path.mkdir()
                    return result

                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        mock.patch.object(merge_gate, "finish_gate_job", side_effect=finish_and_change), redirect_stdout(output):
                    run = merge_gate.run_suites(self.checkout)
                self.assertTrue(run.passed, output.getvalue())
                self.assertEqual(len(run.jobs), 1)
                if state == "append-fails":
                    self.assertEqual(output.getvalue().count("Test pass ledger unavailable;"), 1)
                else:
                    self.assertEqual(self.pass_records(), [])
                _ = self.git(self.checkout, "restore", "subject.txt")

    def test_reused_modules_do_not_hide_a_red_type_job(self) -> None:
        first = self.run_gate("--tests-only", "--repo", str(self.unit))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_gate_events()
        self.write(self.state / "types-red", "red")
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tests reused from", result.stdout)
        self.assertIn("basedpyright: RED", result.stdout)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()


if __name__ == "__main__":
    _ = unittest.main()
