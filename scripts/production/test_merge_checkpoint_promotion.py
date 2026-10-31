"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

import argparse
import fcntl
from contextlib import redirect_stdout
from io import StringIO
import os
import re
import subprocess
import sys
import time
import unittest
from unittest import mock
from pathlib import Path
from typing import final
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.message import sessions

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_checkpoint import promote, request_from
import merge_checkpoint
from production_units import Production

# Importable once merge_checkpoint has put scripts/lib on the path.


from scripts.production.merge_checkpoint_test_support import DELIVERS, MergeCheckpointFixture, PARALLEL, STARTED, TRIAL, configure_test_environment


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    def test_bump_gate_is_launched_before_local_promotion_and_reused_on_retry(self) -> None:
        class InterruptedPromotion(BaseException):
            pass

        self.prepare_bump_gate()
        self.promotions = str(self.promote)
        self.production_doc()
        checkpoint = self.checkpoint()
        records: list[merge_checkpoint.BumpGateRecord] = []
        original_promote = merge_checkpoint.promote

        def promote_after_gate(request: merge_checkpoint.MergeRequest, tip: str, *, local_only: bool = False) -> None:
            self.assertTrue(local_only)
            self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tip)
            merge_head = self.git("rev-parse", "HEAD")
            path = merge_checkpoint.checkpoint_step_record(self.checkout, merge_head, "bump-gate")
            self.assertTrue(path.exists(), "the gate must be recorded before promotion starts")
            record = merge_checkpoint.read_record(path)
            self.assertEqual(record["tip"], tip)
            self.assertEqual(record["result"], "pending")
            self.assertGreater(record["pid"], 0)
            self.assertIsInstance(sessions.process_liveness(record["pid"], record["proc_start"]),
                                  sessions.LiveSessionProcess)
            run, = self.wait_bump_gate_runs(1)
            self.assertEqual((run.head, run.gate_pid, run.gate_start),
                             (tip, record["pid"], record["proc_start"]))
            records.append(record)
            if len(records) == 1:
                raise InterruptedPromotion
            original_promote(request, tip, local_only=local_only)

        with mock.patch.object(merge_checkpoint, "promote", side_effect=promote_after_gate):
            with self.assertRaises(InterruptedPromotion):
                _ = self.run_checkpoint(checkpoint, "--bump")
            retried = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(retried.process.returncode, 0, retried.process.stdout + retried.process.stderr)
        first, second = records
        self.assertEqual(first, second, "a live same-tip gate must keep its original runner")
        self.assertEqual(len(self.bump_gate_runs()), 1)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), first["tip"])
        self.release_bump_gate()
        self.assertEqual(self.wait_bump_result(first["tip"])["result"], "green")

    def assert_gate_notices_follow_local_promotion(self, *, launch_failure: bool) -> None:
        self.prepare_bump_gate(red=True)
        self.promotions = str(self.promote)
        self.production_doc()
        checkpoint = self.checkpoint()
        if not launch_failure:
            marker = self.state / "stop-before-showrunner-notice"
            marker.touch()
            first = self.run_checkpoint(checkpoint, "--bump")
            self.assertEqual(first.process.returncode, 0, first.process.stdout + first.process.stderr)
            run, = self.wait_bump_gate_runs(1)
            self.release_bump_gate()
            self.wait_bump_gate_exit(run)
            self.assertEqual(self.bump_gate_record(run.head)["result"], "red")
            self.assertEqual(len(self.bump_notices()), 1)
            marker.unlink()
        original_start = merge_checkpoint.start_gate
        original_promote = merge_checkpoint.promote
        original_send = merge_checkpoint.send_result
        delivered: list[str] = []

        def start(request: merge_checkpoint.MergeRequest, paths: tuple[str, ...], merge_head: str, tip: str) -> None:
            if launch_failure:
                with self.refuse_bump_gate_launch():
                    original_start(request, paths, merge_head, tip)
            else:
                original_start(request, paths, merge_head, tip)

        def local(request: merge_checkpoint.MergeRequest, tip: str, *, local_only: bool = False) -> None:
            self.assertTrue(local_only)
            path = merge_checkpoint.checkpoint_step_record(self.checkout, self.git("rev-parse", "HEAD"), "bump-gate")
            self.assertEqual(merge_checkpoint.read_record(path)["result"], "red")
            self.assertEqual(len(self.bump_notices()), 0 if launch_failure else 1,
                             "completed gate notices must wait until after local promotion")
            original_promote(request, tip, local_only=local_only)

        def send(path: Path, record: merge_checkpoint.BumpGateRecord) -> None:
            self.assert_main_push_lock_available()
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), record["tip"])
            delivered.append(record["tip"])
            original_send(path, record)

        with mock.patch.object(merge_checkpoint, "start_gate", side_effect=start), \
                mock.patch.object(merge_checkpoint, "promote", side_effect=local), \
                mock.patch.object(merge_checkpoint, "send_result", side_effect=send):
            result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertEqual(delivered, [self.git("rev-parse", "HEAD")])
        gate_path = merge_checkpoint.checkpoint_step_record(self.checkout, self.git("rev-parse", "HEAD"), "bump-gate")
        gate_record = merge_checkpoint.read_record(gate_path)
        self.assertEqual(len(self.bump_notices()), 2, Path(gate_record["log"]).read_text())
        self.assertEqual(len(self.bump_gate_runs()), 0 if launch_failure else 1)

    def test_launcher_failure_notices_wait_for_promotion_and_push_lock_release(self) -> None:
        self.assert_gate_notices_follow_local_promotion(launch_failure=True)

    def test_completed_gate_retry_notices_wait_for_promotion_and_push_lock_release(self) -> None:
        self.assert_gate_notices_follow_local_promotion(launch_failure=False)

    def assert_bump_gate_survives_promotion_failure(self, *, mac_failure: bool) -> None:
        self.prepare_bump_gate(red=True, blocked=False)
        self.promotions = str(self.promote)
        if mac_failure:
            self.promotions += ", mac " + str(self.root / "mac-installed")
            _ = (self.state / "mac-rc").write_text("1\n")
        else:
            self.write(self.promote, "src/alpha.txt", "local edit\n")
        self.production_doc()
        result = self.run_checkpoint(self.checkpoint(), "--bump")
        self.assertEqual(result.process.returncode, 1, result.process.stdout)
        self.assertIn("promote: failed", result.process.stdout)
        tip = self.git("rev-parse", "production", cwd=self.origin)
        run, = self.wait_bump_gate_runs(1)
        self.assertEqual(run.head, tip)
        self.assertEqual(self.wait_bump_result(tip)["result"], "red")
        self.assertEqual(len(self.bump_notices()), 2)

    def test_local_promotion_failure_still_runs_and_reports_the_bump_gate(self) -> None:
        self.assert_bump_gate_survives_promotion_failure(mac_failure=False)

    def test_mac_promotion_failure_still_runs_and_reports_the_bump_gate(self) -> None:
        self.assert_bump_gate_survives_promotion_failure(mac_failure=True)

    def assert_promotion_releases_lock_before_notices_and_mac(self, *, handoff: bool) -> None:
        self.promotions = str(self.promote) + ", mac " + str(self.root / "mac-installed")
        self.production_doc()
        if handoff:
            _ = self.child_production()
        checkpoint = self.checkpoint()
        calls: list[str] = []
        original_into = merge_checkpoint.into

        def check_lock(name: str) -> None:
            self.assert_main_push_lock_available()
            calls.append(name)

        def announce(path: Path, branch: str, reason: str, gates: str) -> None:
            check_lock("into")
            original_into(path, branch, reason, gates)

        def pull(mac: Path, source: Path) -> None:
            self.assertEqual(mac, self.root / "mac-installed")
            self.assertEqual(source, self.promote)
            check_lock("mac")

        def zone() -> None:
            check_lock("zone")

        with mock.patch.object(merge_checkpoint, "into", side_effect=announce), \
                mock.patch.object(merge_checkpoint, "pull_mac_checkout", side_effect=pull), \
                mock.patch.object(merge_checkpoint, "send_queued_zone", side_effect=zone):
            result = self.run_handoff(checkpoint) if handoff else self.run_checkpoint(checkpoint).process
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("into", calls)
        self.assertEqual(calls.count("mac"), 1)
        self.assertEqual(calls.count("zone"), 1)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), self.git("rev-parse", "HEAD"))

    def test_checkpoint_releases_main_push_lock_before_notices_and_mac(self) -> None:
        self.assert_promotion_releases_lock_before_notices_and_mac(handoff=False)

    def test_handoff_releases_main_push_lock_before_notices_and_mac(self) -> None:
        self.assert_promotion_releases_lock_before_notices_and_mac(handoff=True)

    def assert_interrupted_promotion_keeps_new_main_fix(self, *, handoff: bool) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        if handoff:
            _ = self.child_production()
        checkpoint = self.checkpoint()
        with mock.patch.object(merge_checkpoint, "promote", side_effect=merge_checkpoint.Stop(
                "promote", "failed", "interrupted promotion", "retry")):
            failed = self.run_handoff(checkpoint) if handoff else self.run_checkpoint(checkpoint).process
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        pushed = self.git("rev-parse", "production", cwd=self.origin)
        test_events = self.events("checkpoint-test")
        fix = self.push_main_change("fast-fix.txt", "keep this fast fix\n")
        resumed = self.run_handoff(checkpoint) if handoff else self.run_checkpoint(checkpoint).process
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        tip = self.git("rev-parse", "main", cwd=self.origin)
        self.assertNotEqual(tip, pushed)
        self.assertEqual(self.git("merge-base", "--is-ancestor", fix, tip), "")
        self.assertEqual(self.git("merge-base", "--is-ancestor", checkpoint, tip), "")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tip)
        self.assertEqual(self.git("show", "main:fast-fix.txt", cwd=self.origin), "keep this fast fix")
        self.assertEqual(self.events("checkpoint-test"), test_events)

    def test_checkpoint_retry_integrates_main_fix_before_finishing_promotion(self) -> None:
        self.assert_interrupted_promotion_keeps_new_main_fix(handoff=False)

    def test_handoff_retry_integrates_main_fix_before_finishing_promotion(self) -> None:
        self.assert_interrupted_promotion_keeps_new_main_fix(handoff=True)

    def test_fast_main_push_waits_until_checkpoint_promotion_push_finishes(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        lock_path = common / "main-push.lock"
        writer = self.root / "fast-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(writer), cwd=self.root)
        attempted = self.root / "fast-push-contended"
        child_script = '''import fcntl
from pathlib import Path
import subprocess
import sys
lock, marker, checkout = map(Path, sys.argv[1:])
with lock.open("a") as held:
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        marker.write_text("contended")
    else:
        marker.write_text("unlocked")
    fcntl.flock(held, fcntl.LOCK_EX)
    for args in (("fetch", "origin", "main"), ("reset", "--hard", "origin/main")):
        subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True)
    (checkout / "fast-fix.txt").write_text("fast fix after promotion\\n")
    subprocess.run(["git", "add", "fast-fix.txt"], cwd=checkout, check=True)
    subprocess.run(["git", "-c", "user.name=Fast Test", "-c", "user.email=fast@example.invalid",
                    "commit", "-m", "fast fix"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=checkout, check=True, capture_output=True)
'''
        children: list[subprocess.Popen[str]] = []
        original_promote = merge_checkpoint.promote_local_checkout
        original_push = merge_checkpoint.push_git
        started: list[float] = []
        spans: list[float] = []

        def push(production: Production, may_undo: bool) -> str:
            started.append(time.monotonic())
            return original_push(production, may_undo)

        def promote_with_contender(checkout: Path, branch: str, tip: str) -> (
                merge_checkpoint.PromotionAlreadyAtTip | merge_checkpoint.PromotionOriginPushed |
                merge_checkpoint.PromotionAdvanced | merge_checkpoint.PromotionNotPushed):
            with lock_path.open("a") as held:
                with self.assertRaises(BlockingIOError, msg="promotion must hold the common push lock"):
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            process = subprocess.Popen([sys.executable, "-c", child_script, str(lock_path), str(attempted), str(writer)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            children.append(process)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            wait_until(lambda: attempted.exists() and attempted.read_text() != "", self.root,
                       processes=(process,))
            self.assertEqual(attempted.read_text(), "contended")
            result = original_promote(checkout, branch, tip)
            spans.append(time.monotonic() - started[0])
            return result

        with mock.patch.object(merge_checkpoint, "push_git", side_effect=push), \
                mock.patch.object(merge_checkpoint, "promote_local_checkout", side_effect=promote_with_contender):
            result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        for line in result.process.stdout.splitlines():
            if "main push lock:" in line:
                print(line)
        child, = children
        output, errors = child.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(child.returncode, 0, output + errors)
        self.assertEqual(self.git("merge-base", "--is-ancestor", sha, "main", cwd=self.origin), "")
        self.assertEqual(self.git("show", "main:fast-fix.txt", cwd=self.origin), "fast fix after promotion")
        print(f"main-push protected span={spans[0]:.6f}s (push fetch through promotion push)")

    def test_rerun_after_origin_main_moves_skips_merge_branch_push_and_promotes_pushed_tip(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        head = self.git("rev-parse", "HEAD")
        writer = self.root / "main-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(writer), cwd=self.root)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=writer)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=writer)
        self.write(writer, "later-main.txt", "main moved after push\n")
        _ = self.git("add", "later-main.txt", cwd=writer)
        _ = self.git("commit", "-m", "later main", cwd=writer)
        _ = self.git("push", "origin", "main", cwd=writer)
        rerun = self.run_checkpoint(sha)
        self.assertEqual(rerun.process.returncode, 0, rerun.process.stderr + rerun.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), pushed)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), pushed)
        self.assertIn(f"already pushed at {pushed}", rerun.process.stdout)
    def test_retry_pushes_main_after_initial_promotion_push_rejection(self) -> None:
        self.promotions = str(self.promote) + " — live checkout"
        self.production_doc()
        marker = self.root / "reject-main"
        _ = marker.touch()
        hook = ("#!/bin/sh\nwhile read old new ref; do\n"
                + f'  if [ "$ref" = "refs/heads/main" ] && [ -e "{marker}" ]; then exit 1; fi\n'
                + "done\nexit 0\n")
        self.executable(self.origin / "hooks/pre-receive", hook)
        sha = self.checkpoint()
        failed = self.run_checkpoint(sha)
        self.assertEqual(failed.process.returncode, 1)
        self.assertIn("promote: failed", failed.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), pushed)
        self.assertNotEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), pushed)
        marker.unlink()
        retried = self.run_checkpoint(sha)
        self.assertEqual(retried.process.returncode, 0, retried.process.stderr + retried.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), pushed)
        self.assertIn(f"already pushed at {pushed}", retried.process.stdout)
        self.assertIn(f"promote: ok — {self.promote} already at {pushed}\n", retried.process.stdout)
    def test_local_foreign_main_commit_never_reaches_origin(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        self.write(self.promote, "foreign-main.txt", "another session\n")
        _ = self.git("add", "foreign-main.txt", cwd=self.promote)
        _ = self.git("commit", "-m", "foreign main commit", cwd=self.promote)
        origin_main = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 1)
        self.assertIn("promote: failed", result.process.stdout)
        self.assertIn(str(self.promote), result.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), origin_main)
    def test_rerun_never_pushes_a_main_that_holds_a_foreign_commit(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.write(self.promote, "foreign-main.txt", "another session\n")
        _ = self.git("add", "foreign-main.txt", cwd=self.promote)
        _ = self.git("commit", "-m", "foreign main commit", cwd=self.promote)
        rerun = self.run_checkpoint(sha)
        self.assertEqual(rerun.process.returncode, 0, rerun.process.stderr + rerun.process.stdout)
        self.assertIn(f"promote: ok — {self.promote} main at {pushed}; "
                      + "main holds commits not on origin/production and was not pushed\n",
                      rerun.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), pushed)
    def test_promote_at_tip_with_foreign_remote_history_says_already_at_without_push(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        tip = self.git("rev-parse", "HEAD")
        base = self.git("rev-parse", "HEAD^1")
        _ = self.git("push", "--force", "origin", f"{base}:refs/heads/main")
        _ = self.git("push", "--force", "origin", f"{base}:refs/heads/production")
        with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):  # Merge tests names a stub on the test PATH
            request = request_from(argparse.Namespace(
                production=str(self.doc), unit="alpha-unit", phase="2", hash=sha,
                also=[], shrink=False, as_built=False, trailer=[], review_trial=TRIAL, delivers=DELIVERS, parallel=PARALLEL,
                cancel_prior=False, started=STARTED, regime="after", holds=0,
                merge_defects=0, excluded=None, scratch=str(self.root / "scratch"),
            ))
        output = StringIO()
        with redirect_stdout(output):
            promote(request, tip)
        self.assertIn(f"promote: ok — {self.promote} already at {tip}; "
                      + "main holds commits not on origin/production and was not pushed\n", output.getvalue())
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), base)
    def test_promote_reports_already_at_tip_when_both_mains_match(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        second = self.run_checkpoint(sha)
        self.assertEqual(second.process.returncode, 0, second.process.stderr + second.process.stdout)
        self.assertIn(f"promote: ok — {self.promote} already at {tip}\n", second.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)
    def test_git_push_merges_diverged_main_promotes_pushed_tip_and_records(self) -> None:
        self.promotions = str(self.promote) + ", mac " + str(self.root / "mac-installed")
        self.production_doc()
        sha = self.checkpoint()
        main_writer = self.root / "main-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(main_writer), cwd=self.root)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=main_writer)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=main_writer)
        self.write(main_writer, "main-only.txt", "another session\n")
        _ = self.git("add", "main-only.txt", cwd=main_writer)
        _ = self.git("commit", "-m", "main moved", cwd=main_writer)
        _ = self.git("push", "origin", "main", cwd=main_writer)
        result = self.run_checkpoint(sha, "--holds", "2", "--merge-defects", "1",
                                     "--regime", "trial", "--excluded", "no merge design check")
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), tip)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)
        self.assertIn(f"promote: ok — {self.promote} main at {tip}\n", result.process.stdout)
        self.assertIn(f"promote: ok — mac {self.root / 'mac-installed'} main pulled\n",
                      result.process.stdout)
        self.assertEqual(self.git("merge-base", "--is-ancestor", sha, tip), "")
        self.assertIn("main-only.txt", self.git("ls-tree", "-r", "--name-only", tip))
        receipts = self.receipts()
        self.assertEqual({row["merge_commit"] for row in receipts},
                         set(self.git("rev-list", "--first-parent", "--merges", "HEAD").splitlines()))
        integration, = [row for row in receipts if row["merge_commit"] == tip]
        self.assertEqual(integration["source_checkpoint"], self.git("rev-parse", "HEAD^2"))
        self.assertRegex(result.process.stdout, re.escape(f"into: {self.checkout} (production) — ") + ".*unblocks: G1: beta-unit Phase 4")
        self.assertRegex(result.process.stdout, re.escape(f"into: {self.promote} (main) — ") + ".*unblocks: the live gate of alpha-unit phase 2")
        self.assertIn("the merge branch collects every unit's checkpoints", result.process.stdout)
        self.assertIn("sessions on", result.process.stdout)
        self.assertIn("into: " + str(self.root / "mac-installed"), result.process.stdout)
        self.assertEqual(len(self.events("ssh")), 1)
        review = self.events("review_regime.py")
        self.assertEqual(len(review), 1)
        merged_at = review[0][review[0].index("--merged") + 1]
        self.assertRegex(merged_at, r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(review[0], [
            "add", "--unit", "alpha-unit", "--plan", "docs/plans/alpha.md", "--phase", "2", "--regime", "trial",
            "--started", STARTED, "--merged", merged_at, "--holds", "2",
            "--merge-defects", "1", "--ux-findings", "2", "--code-findings", "3",
            "--review-minutes", "14", "--ux-check-minutes", "4",
            "--ux-repair-minutes", "1", "--excluded", "no merge design check",
        ])
        self.assertRegex(self.log.read_text(), r"- \d\d:\d\d PDT: alpha-unit phase 2 \([0-9a-f]+\) merged as [0-9a-f]+; .*green; pushed; promoted")
        self.assertIn("send alpha-unit:", result.process.stdout)
    def test_promotion_refusal_keeps_pushed_merge_and_resume_reaches_only_missing_place(self) -> None:
        self.promotions = str(self.promote)
        self.gate = False
        self.production_doc()
        sha = self.checkpoint()
        self.write(self.promote, "src/alpha.txt", "local edit\n")
        failed = self.run_checkpoint(sha)
        self.assertIn("failed", failed.process.stdout)
        self.assertIn("src/alpha.txt", failed.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertEqual(self.git("rev-parse", "HEAD"), pushed)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), failed.before)
        self.assertIn(f"into: {self.checkout} (production)", failed.process.stdout)
        self.assertIn("unblocks: no gate", failed.process.stdout)
        self.assertNotIn(f"into: {self.promote} (main)", failed.process.stdout)
        _ = self.git("restore", "src/alpha.txt", cwd=self.promote)
        resumed = self.run_checkpoint(sha)
        self.assertEqual(resumed.process.returncode, 0, resumed.process.stderr + resumed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), pushed)
        self.assertIn(f"into: {self.promote} (main)", resumed.process.stdout)
        self.assertNotIn("unblocks: G1", resumed.process.stdout)
    def test_mac_rc_failure_is_reported_after_local_promotion(self) -> None:
        self.promotions = str(self.promote) + ", mac " + str(self.root / "mac-installed")
        self.production_doc()
        _ = (self.state / "mac-rc").write_text("1\n")
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertIn("failed", result.process.stdout)
        self.assertIn(f"into: {self.checkout} (production)", result.process.stdout)
        self.assertIn(f"into: {self.promote} (main)", result.process.stdout)
        self.assertNotIn("into: " + str(self.root / "mac-installed"), result.process.stdout)
    def test_record_failure_resumes_after_push_without_second_merge(self) -> None:
        sha = self.checkpoint()
        _ = (self.state / "fail-review_regime.py").touch()
        failed = self.run_checkpoint(sha)
        self.assertIn("record: failed", failed.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertNotEqual(pushed, failed.before)
        self.assertFalse(self.log.exists())
        (self.state / "fail-review_regime.py").unlink()
        resumed = self.run_checkpoint(sha)
        self.assertEqual(resumed.process.returncode, 0, resumed.process.stderr + resumed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), pushed)
        self.assertEqual(len(self.events("checkpoint-test")), 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn("unblocks: no gate", resumed.process.stdout)
    def test_a_merge_killed_before_its_push_is_tested_before_a_rerun_pushes_it(self) -> None:
        # Killed after the merge and before its tests finished: nothing proves the merge green.
        sha = self.checkpoint()
        before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        subject = f"Merge alpha-unit phase 2 ({sha[:7]}) into production"
        _ = self.git("merge", "--no-ff", "-q", "-m", subject, sha, cwd=self.checkout)
        _ = (self.state / "fail-checkpoint-test").touch()
        red = self.run_checkpoint(sha)
        self.assertEqual(red.process.returncode, 1, red.process.stderr + red.process.stdout)
        self.assertIn("red: failed", red.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)
        (self.state / "fail-checkpoint-test").unlink()
        green = self.run_checkpoint(sha)
        self.assertEqual(green.process.returncode, 0, green.process.stderr + green.process.stdout)
        self.assertNotEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)
    def test_promote_none_merges_and_records_without_promotion(self) -> None:
        self.promotions = "none"
        self.production_doc()
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("promote: ok — no promotion configured", result.process.stdout)
        self.assertIn("record: ok", result.process.stdout)
    def test_a_missing_promote_checkout_is_refused_before_anything_is_pushed(self) -> None:
        self.promotions = str(self.unit / "no-such-checkout")
        self.production_doc()
        before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        result = self.run_checkpoint(self.checkpoint())
        self.assertNotEqual(result.process.returncode, 0)
        self.assertIn("no-such-checkout does not exist", result.process.stdout + result.process.stderr)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)


if __name__ == "__main__":
    _ = unittest.main()
