"""Merge gate checkpoint contracts."""

from __future__ import annotations

import unittest

import json
import io
import os
from pathlib import Path
import re
from typing import cast, final
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import change_review, merge_gate
from scripts.production.merge_gate_test_support import EXPECTED_SUITES, MergeGateFixture, gate_suites


@final
class MergeGateCheckpointTests(MergeGateFixture):
    def test_checkpoint_retries_its_merge_after_a_stop_before_push(self) -> None:
        checked = merge_gate.checked_git

        def stop_at_push(repo: Path, *arguments: str) -> str:
            if arguments == ("push", "origin", "integration"):
                raise merge_gate.GateRefused("injected stop before push")
            return checked(repo, *arguments)

        with mock.patch.object(merge_gate, "checked_git", side_effect=stop_at_push):
            stopped = self.run_gate()
        self.assertNotEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("injected stop before push", stopped.stderr)
        merged = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertNotEqual(merged, self.base)
        self.assert_not_pushed()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), merged)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), merged)
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [merged, self.base, self.checkpoint])

    def test_checkpoint_retry_refuses_a_remote_rewound_below_its_base(self) -> None:
        self.write(self.checkout / "published.txt", "published code\n")
        published = self.commit(self.checkout, "published non-doc change")
        _ = self.git(self.checkout, "push", "origin", "integration")
        checked = merge_gate.checked_git

        def stop_at_push(repo: Path, *arguments: str) -> str:
            if arguments == ("push", "origin", "integration"):
                raise merge_gate.GateRefused("injected stop before push")
            return checked(repo, *arguments)

        with mock.patch.object(merge_gate, "checked_git", side_effect=stop_at_push):
            stopped = self.run_gate()
        self.assertNotEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("injected stop before push", stopped.stderr)
        merged = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [merged, published, self.checkpoint])
        _ = self.git(self.origin, "update-ref", "refs/heads/integration", self.base, published)

        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("merge HEAD differs from origin merge branch", result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), merged)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), self.base)

    def test_checkpoint_retry_refuses_a_recorded_merge_for_another_checkpoint(self) -> None:
        checked = merge_gate.checked_git

        def stop_at_push(repo: Path, *arguments: str) -> str:
            if arguments == ("push", "origin", "integration"):
                raise merge_gate.GateRefused("injected push failure")
            return checked(repo, *arguments)

        with mock.patch.object(merge_gate, "checked_git", side_effect=stop_at_push):
            stopped = self.run_gate()
        self.assertIn("injected push failure", stopped.stderr)
        merged = self.git(self.checkout, "rev-parse", "HEAD")
        self.write(self.unit / "next.txt", "next checkpoint\n")
        checkpoint = self.commit(self.unit, "next checkpoint")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        result = self.run_gate(checkpoint=checkpoint)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), merged)
        self.assert_not_pushed()

    def test_dirty_merge_checkout_is_refused_without_destroying_changes(self) -> None:
        self.write(self.checkout / "subject.txt", "uncommitted\n")
        self.assert_refused_before_gate(self.run_gate())
        self.assertEqual((self.checkout / "subject.txt").read_text(), "uncommitted\n")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)

    def test_untracked_showrunner_log_does_not_block_the_gate(self) -> None:
        self.write(self.checkout / "docs" / "plans" / "build-followups-log.md", "log\n")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.checkout / "docs" / "plans" / "build-followups-log.md").read_text(), "log\n")

    def test_missing_type_dir_is_refused_before_any_suite_runs(self) -> None:
        result = self.run_gate("--type-dirs", "accounts")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("type dirs not found", result.stderr)
        self.assertIn("accounts", result.stderr)
        self.assertEqual(self.module_events(), [])
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)

    def test_merge_checkout_ahead_of_origin_is_refused(self) -> None:
        self.write(self.checkout / "local.txt", "local\n")
        local = self.commit(self.checkout, "unpublished merge work")
        self.assert_refused_before_gate(self.run_gate())
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), local)

    def test_a_local_production_doc_commit_goes_out_with_the_merge(self) -> None:
        # retire_unit, add_unit and promote_showrunner leave their doc commit to the next merge push.
        self.write(self.checkout / "local.txt", "retired\n")
        local = self.commit(self.checkout, "production(fixture): retire alpha-unit")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Local doc commit goes out with this merge: {local[:12]}", result.stdout)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), head)
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [head, local, self.checkpoint])

    def test_a_red_gate_keeps_the_local_production_doc_commit(self) -> None:
        self.write(self.checkout / "local.txt", "retired\n")
        local = self.commit(self.checkout, "production(fixture): retire alpha-unit")
        self.write(self.state / "modify-tracked", "modify")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), local)
        self.assert_not_pushed()

    def test_merge_checkout_behind_origin_is_refused(self) -> None:
        _ = self.git(self.unit, "push", "origin", "HEAD:integration")
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), self.checkpoint)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.calls(), [])

    def test_wrong_checkout_branch_is_refused(self) -> None:
        _ = self.git(self.checkout, "checkout", "-b", "wrong-branch")
        self.assert_refused_before_gate(self.run_gate())

    def test_checkpoint_not_on_remote_unit_branch_is_refused(self) -> None:
        self.write(self.unit / "unpublished.txt", "unpublished\n")
        unpublished = self.commit(self.unit, "unpublished checkpoint")
        self.assert_refused_before_gate(self.run_gate(checkpoint=unpublished))

    def test_missing_checkpoint_is_refused(self) -> None:
        self.assert_refused_before_gate(self.run_gate(checkpoint="does-not-exist"))

    def test_missing_previous_checkpoint_is_refused(self) -> None:
        self.assert_refused_before_gate(self.run_gate(previous="does-not-exist"))

    def test_previous_checkpoint_must_be_an_ancestor(self) -> None:
        self.write(self.unit / "later.txt", "later\n")
        later = self.commit(self.unit, "later checkpoint")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        self.assert_refused_before_gate(self.run_gate(previous=later))

    def test_conflict_aborts_merge_and_names_conflicting_files(self) -> None:
        self.write(self.unit / "subject.txt", "unit version\n")
        self.checkpoint = self.commit(self.unit, "unit conflict")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        self.write(self.checkout / "subject.txt", "merge version\n")
        self.base = self.commit(self.checkout, "merge conflict")
        _ = self.git(self.checkout, "push", "origin", "integration")
        result = self.run_gate(previous=self.git(self.unit, "rev-parse", "HEAD~1"))
        self.assert_refused_before_gate(result)
        self.assertIn("subject.txt", result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assertFalse((self.checkout / ".git" / "MERGE_HEAD").exists())

    def test_a_checkpoint_without_a_change_review_line_is_refused_before_the_merge(self) -> None:
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades")
        result = self.run_gate(checkpoint=checkpoint)
        self.assert_refused_before_gate(result)
        self.assertIn("checkpoint(fixture): grid fades: no Change-Review line", result.stderr)

    def test_a_green_merge_adds_one_row_per_visible_change_to_the_named_list(self) -> None:
        review, doc = self.review_list()
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\n"
                                            + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away\n"
                                            + "Change-Review: none — the gate script")
        # The row carries today in the user's zone, as the gate reads it; a run across midnight may take either day.
        before = change_review.today()
        result = self.run_gate("--production", str(doc), checkpoint=checkpoint)
        days = {before.isoformat(), change_review.today().isoformat()}
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merged = self.git(self.checkout, "rev-parse", "--short=8", "HEAD")
        written = review.read_text(encoding="utf-8").splitlines()[-1]
        rows = {f"| C8 | The grid fades gently | {day} · {merged} | not yet | Run `hana` in a natedev terminal. You see: The grid lines fade out farther away | production |" for day in days}
        self.assertIn(written, rows)
        self.assertIn(f"Change review: {written}", result.stdout)
        self.assertNotIn("drift", result.stderr)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_without_production_the_calling_showrunners_list_takes_the_rows(self) -> None:
        review, doc = self.review_list()
        timer = Path(self.environment["NOTIFIER_STATE_DIR"]) / "showrunner-fixture"
        timer.mkdir(parents=True)
        self.write(timer / "conf", "TARGET=session:fixture-showrunner\n"
                   + f"CHECK=bash /scripts/production/production_check.sh {doc}\n")
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\n"
                                            + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away\n"
                                            + "Change-Review: The sky dims | Run `hana` in a natedev terminal | The sky turns darker")
        result = self.run_gate(checkpoint=checkpoint, callers_production=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Change review: 2 row(s) added to {review}: C8–C9", result.stdout)
        self.assertEqual([line.split(" | ")[1] for line in review.read_text(encoding="utf-8").splitlines()[-2:]],
                         ["The grid fades gently", "The sky dims"])

    def test_a_gate_that_finds_no_production_is_refused_before_the_merge(self) -> None:
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\n"
                                            + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away")
        result = self.run_gate(checkpoint=checkpoint, callers_production=True)
        self.assert_refused_before_gate(result)
        self.assertIn("pass --production <doc>: the gate adds this merge's change-review rows to its list",
                      result.stderr)

    def test_a_production_without_a_list_still_merges_a_checkpoint_with_its_line(self) -> None:
        doc = self.root / "production.md"
        self.write(doc, "# p\n")
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\nChange-Review: none — the gate script")
        result = self.run_gate("--production", str(doc), checkpoint=checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Change review: the production doc has no Change review line; no rows written",
                      result.stdout)

    @gate_suites(*EXPECTED_SUITES)
    def test_green_gate_pushes_no_ff_merge_with_subject_body_and_all_modules(self) -> None:
        self.write(self.state / "types-barrier", "overlap")
        self.write(self.state / "types-hold", "hold")
        memory = merge_gate.process_memory_mib

        def sample_and_release(roots: set[int]) -> dict[int, float]:
            measured = memory(roots)
            for call in self.calls():
                if call["tool"] == "basedpyright" and measured.get(call["pid"], 0) > 0:
                    (self.state / "types-sampled").touch()
            return measured

        with mock.patch.object(merge_gate, "process_memory_mib", side_effect=sample_and_release):
            result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), head)
        notifications = [call["args"] for call in self.calls() if call["tool"] == "style_sync.py"]
        self.assertEqual(notifications, [["notify", "--repo-dir", str(self.checkout), "--branch", "integration", "--tip", head]])
        self.assertEqual((self.state / "notified-tip").read_text(), head)
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [head, self.base, self.checkpoint])
        message = self.git(self.checkout, "log", "-1", "--format=%B")
        self.assertIn(f"Merge fixture-unit phase-2 ({self.checkpoint}) into integration", message)
        self.assertIn("Fixture checkpoint gate.", message)
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual([(row["suite"], row["module"]) for row in starts],
                              [(suite, "test_first.py") for suite in self.suites] + ([("lib", "test_second.py")] if "lib" in self.suites else []))
        self.assertEqual(len({row["pid"] for row in starts}), self.module_count)
        self.assertEqual(len({row["tmpdir"] for row in starts}), self.module_count)
        self.assertTrue(all(not Path(row["tmpdir"]).exists() for row in starts))
        type_calls = [call for call in self.calls() if call["tool"] == "basedpyright"]
        self.assertEqual(len(type_calls), 1)
        venv = self.state / "runner" / "typecheck-venv"
        self.assertEqual(type_calls[0]["args"], ["--pythonpath", str(venv / "bin" / "python"), "scripts/production"])
        peaks = cast(dict[str, float], json.loads((self.state / "runner" / "types_peak_mib.json").read_text()))
        self.assertEqual(list(peaks), ["scripts/production"])
        self.assertGreater(peaks["scripts/production"], 0)
        self.assertEqual(type_calls[0]["cwd"], str(self.checkout))
        for suite in self.suites:
            self.assertRegex(result.stdout, rf"(?m)^{suite}: OK \d+\.\d+s$")
        peak = re.search(r"peak memory: ([\d.]+) GiB", result.stdout)
        self.assertIsNotNone(peak, result.stdout)
        assert peak is not None
        self.assertGreater(float(peak.group(1)), 0)
        self.assertRegex(result.stdout, r"(?m)^Largest module peak: [\d.]+ GiB \(sampled memory\); module: (?:\w+/test_\w+\.py|\(not sampled\))$")
        launches = [call for call in self.calls() if call["tool"] == "suite-python"]
        self.assertEqual(len(launches), self.module_count)
        for call in launches:
            self.assertIn("discover", call["args"])
            self.assertEqual(call["cwd"], str(self.checkout))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_rejected_push_never_notifies_style(self) -> None:
        self.write(self.origin / "hooks/pre-receive", "#!/bin/sh\nexit 1\n")
        (self.origin / "hooks/pre-receive").chmod(0o755)
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertFalse(any(call["tool"] == "style_sync.py" for call in self.calls()))

    def test_style_notification_failure_keeps_the_green_merge_successful(self) -> None:
        self.write(self.state / "notify-fails", "fail")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("style sync: exit 1", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "integration"))

    def test_notification_value_error_keeps_the_completed_merge_successful(self) -> None:
        original_notify = merge_gate.notify_style

        def malformed_notification(repo: Path, branch: str, tip: str) -> None:
            with mock.patch("scripts.production.merge_gate.subprocess.run",
                            side_effect=ValueError("invalid production")):
                original_notify(repo, branch, tip)

        arguments = ["--repo", str(self.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint,
                     "--previous-checkpoint", self.base, "--unit", "fixture-unit", "--phase", "phase-2",
                     "--body", "Fixture checkpoint gate.", "--type-dirs", "scripts/production",
                     "--settings-blob", self.settings_blob, "--main-repo", str(self.main),
                     "--ssh", str(self.bin / "ssh"), "--production", str(self.quiet_production)]
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(
            merge_gate, "notify_style", side_effect=malformed_notification,
        ), redirect_stdout(output):
            code = merge_gate.main(arguments)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("style sync: invalid production", output.getvalue())
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "integration"))


if __name__ == "__main__":
    _ = unittest.main()
