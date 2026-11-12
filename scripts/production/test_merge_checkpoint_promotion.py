"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

from scripts.production import checkpoint_gate

import argparse
import fcntl
from collections.abc import Generator
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, tzinfo
from io import StringIO
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path, PurePosixPath
from typing import final
from scripts.production.cli_fixture import production_paths
from scripts.agents import pane_record
from zoneinfo import ZoneInfo
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib import main_publish
from scripts.message import sessions

from scripts.production.merge_checkpoint import promote, request_from
from scripts.production import merge_checkpoint
from scripts.production import change_review
from scripts.production.production_doc import Production

# Importable once merge_checkpoint has put scripts/lib on the path.


from scripts.production.merge_checkpoint_test_support import DELIVERS, MergeCheckpointFixture, STARTED, STUB, TRIAL, configure_test_environment


MERGED_AT = datetime(2026, 10, 6, 12, tzinfo=ZoneInfo("America/Los_Angeles"))


def setUpModule() -> None:
    configure_test_environment()
    state = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(state.cleanup)
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"ZONE_STATE": state.name}))


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    def assert_completed_handoff_preserves_published_tip(self, *, with_review: bool) -> None:
        review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
        if with_review:
            self.promotions = str(self.promote)
            self.production_doc(change_review=review)
        _ = self.child_production()
        checkpoint = self.dated_checkpoint(
            "checkpoint(alpha): completed hand-off\n\n"
            + "Change-Review: The completed hand-off stays recorded | Open the fixture | The change is listed")
        first = self.run_handoff(checkpoint)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        merge_head = self.git("rev-parse", "HEAD")
        notices = self.events("style_sync.py")
        receipts = self.receipts()
        rows = review.read_bytes() if with_review else b""
        fix = self.push_main_change("later-main.txt", "a later main change\n")
        original_git = merge_checkpoint.git

        def refuse_push(checkout: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
            self.assertNotIn("push", arguments, "a completed hand-off must not push again")
            return original_git(checkout, *arguments)

        with mock.patch.object(merge_checkpoint, "git", side_effect=refuse_push):
            duplicate = self.run_handoff(checkpoint)
        self.assertEqual((duplicate.returncode, duplicate.stdout, duplicate.stderr),
                         (0, f"hand-off {checkpoint[:7]} already merged as {merge_head[:7]}\n", ""))
        self.assertEqual(self.git("rev-parse", "HEAD"), merge_head)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge_head)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), fix)
        self.assertEqual(self.events("style_sync.py"), notices)
        self.assertEqual(self.receipts(), receipts)
        if with_review:
            self.assertEqual(review.read_bytes(), rows)

    def test_completed_handoff_without_review_keeps_its_tip_when_main_advances(self) -> None:
        self.assert_completed_handoff_preserves_published_tip(with_review=False)

    def test_completed_handoff_with_review_keeps_its_tip_when_main_advances(self) -> None:
        self.assert_completed_handoff_preserves_published_tip(with_review=True)

    def test_completed_handoff_repairs_missing_publication_records(self) -> None:
        review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
        self.promotions = str(self.promote)
        self.production_doc(change_review=review)
        _ = self.child_production()
        checkpoint = self.dated_checkpoint(
            "checkpoint(alpha): publication recovery\n\n"
            + "Change-Review: The hand-off records recover | Open the fixture | The change is listed")
        first = self.run_handoff(checkpoint)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        merge_head = self.git("rev-parse", "HEAD")
        receipts = self.receipts()
        notices = self.events("style_sync.py")
        review.unlink()
        (self.root / "history/merge_receipts.jsonl").unlink()
        repaired = self.run_handoff(checkpoint)
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        self.assertIn("change review: ok — 1 row(s) added", repaired.stdout)
        self.assertEqual(review.read_text().count("The hand-off records recover"), 1)
        self.assertEqual(self.receipts(), receipts)
        self.assertEqual(self.events("style_sync.py"), notices)
        duplicate = self.run_handoff(checkpoint)
        self.assertEqual((duplicate.returncode, duplicate.stdout, duplicate.stderr),
                         (0, f"hand-off {checkpoint[:7]} already merged as {merge_head[:7]}\n", ""))
        self.assertEqual(self.receipts(), receipts)

    def test_failed_main_push_adds_no_row_and_a_good_retry_adds_it_once(self) -> None:
        self.promotions = str(self.promote)
        review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
        self.production_doc(change_review=review)
        checkpoint = self.dated_checkpoint("checkpoint(alpha): published\n\nChange-Review: Published | Read | Ready")
        hook = self.origin / "hooks/pre-receive"
        self.executable(hook, '#!/bin/sh\nwhile read old new ref; do\n'
                        + ' if [ "$ref" = refs/heads/main ]; then exit 1; fi\ndone\n')
        failed = self.run_checkpoint(checkpoint)
        self.assertNotEqual(failed.process.returncode, 0, failed.process.stdout + failed.process.stderr)
        self.assertEqual(change_review.pending(review), [])
        published = self.git("rev-parse", "production", cwd=self.origin)
        receipt, = self.receipts()
        self.assertEqual(receipt["merge_commit"], published)
        self.assertEqual(len(self.events("style_sync.py")), 1)
        hook.unlink()
        retried = self.run_checkpoint(checkpoint)
        self.assertEqual(retried.process.returncode, 0, retried.process.stdout + retried.process.stderr)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), published)
        self.assertEqual([row["Change"] for row in change_review.pending(review)], ["Published"])
        written = review.read_bytes()
        self.assertEqual(self.run_checkpoint(checkpoint).process.returncode, 0)
        self.assertEqual(review.read_bytes(), written)
        self.assertEqual(self.receipts(), [receipt])
        self.assertEqual(len(self.events("style_sync.py")), 1)

    def test_failed_style_notice_retries_until_delivery_succeeds(self) -> None:
        checkpoint = self.checkpoint()
        failure = self.state / "fail-style_sync.py"
        failure.touch()
        first = self.run_checkpoint(checkpoint)
        self.assertEqual(first.process.returncode, 0, first.process.stdout + first.process.stderr)
        self.assertIn("style sync: exit 1", first.process.stdout)
        merge_head = self.git("rev-parse", "HEAD")
        marker = merge_checkpoint.checkpoint_step_record(self.checkout, merge_head, "style-notice-" + merge_head)
        self.assertFalse(marker.exists(), "a failed delivery must remain eligible for retry")
        self.assertEqual(len(self.events("style_sync.py")), 1)
        failure.unlink()
        retried = self.run_checkpoint(checkpoint)
        self.assertEqual(retried.process.returncode, 0, retried.process.stdout + retried.process.stderr)
        self.assertTrue(marker.exists(), "a successful delivery must record completion")
        self.assertEqual(len(self.events("style_sync.py")), 2)
        duplicate = self.run_checkpoint(checkpoint)
        self.assertEqual(duplicate.process.returncode, 0, duplicate.process.stdout + duplicate.process.stderr)
        self.assertEqual(len(self.events("style_sync.py")), 2, "a completed delivery must not run again")

    def test_slow_gate_delivery_leaves_push_lock_available_before_retry_launch(self) -> None:
        self.prepare_bump_gate()
        self.promotions = str(self.promote)
        self.production_doc()
        checkpoint = self.checkpoint()
        with self.refuse_bump_gate_launch(), mock.patch.object(merge_checkpoint, "promote", side_effect=(
                checkpoint_gate.Stop("promote", "failed", "fixture promotion failed", "retry"))):
            failed = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(failed.process.returncode, 1, failed.process.stdout + failed.process.stderr)
        merge_head = self.git("rev-parse", "HEAD")
        path = merge_checkpoint.checkpoint_step_record(self.checkout, merge_head, "bump-gate")
        record = checkpoint_gate.read_record(path)
        record["delivered_to"] = []
        checkpoint_gate.save_record(path, record)
        fix = self.push_main_change("fast-fix.txt", "new main tip\n")
        (self.state / "slow-delivery").touch()
        command = (
            "import sys\nfrom pathlib import Path\n"
            'with open(Path(sys.argv[1]).parents[1] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})\nfrom scripts.production import checkpoint_gate as checkpoint\n'
            "path = Path(sys.argv[2])\ncheckpoint.send_result(path, checkpoint.read_record(path))\n"
        )
        output = self.enterContext((self.state / "slow-delivery.log").open("w"))
        sender = subprocess.Popen([sys.executable, "-c", command, str(Path(merge_checkpoint.__file__).parent), str(path)],
                                  cwd=self.checkout, env=self.env, stdout=output, stderr=subprocess.STDOUT)

        def stop_sender() -> None:
            (self.state / "delivery-release").touch()
            _ = sender.wait(timeout=TEST_HANG_GUARD_SECS)

        self.addCleanup(stop_sender)
        wait_until(lambda: (self.state / "delivery-started").exists(), self.state, processes=(sender,))
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        retry_waits = threading.Event()
        failures: list[BaseException] = []
        original_lock = checkpoint_gate.record_lock

        @contextmanager
        def observe_lock(record_path: Path) -> Generator[None]:
            retry_waits.set()
            with original_lock(record_path):
                yield

        def probe_push_lock() -> None:
            try:
                self.assertTrue(retry_waits.wait(TEST_HANG_GUARD_SECS), "the retry must reach the record lock")
                with (common / "main-push.lock").open("a") as held:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(held, fcntl.LOCK_UN)
            except BaseException as error:
                failures.append(error)
            finally:
                (self.state / "delivery-release").touch()

        probe = threading.Thread(target=probe_push_lock)
        probe.start()
        try:
            with mock.patch.object(checkpoint_gate, "record_lock", side_effect=observe_lock):
                retried = self.run_checkpoint(checkpoint, "--bump")
        finally:
            probe.join(timeout=TEST_HANG_GUARD_SECS)
        self.assertFalse(probe.is_alive(), "the push lock probe must finish")
        self.assertEqual(failures, [], "slow gate delivery must leave the push lock available")
        self.assertEqual(retried.process.returncode, 0, retried.process.stdout + retried.process.stderr)
        self.assertEqual(sender.wait(timeout=TEST_HANG_GUARD_SECS), 0,
                         (self.state / "slow-delivery.log").read_text())
        run, = self.wait_bump_gate_runs(1)
        self.assertEqual(self.git("merge-base", "--is-ancestor", fix, run.head), "")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), run.head)
        self.release_bump_gate()
        completed = self.wait_bump_result_at(path)
        self.assertEqual(completed["tip"], run.head)
        self.assertEqual(completed["result"], "green")

    def assert_publication_records_survive_promotion_failure(self, *, mac_failure: bool, handoff: bool) -> None:
        self.promotions = str(self.promote)
        if mac_failure:
            self.promotions += ", mac " + str(self.root / "mac-installed")
            _ = (self.state / "mac-rc").write_text("1\n")
        else:
            self.write(self.promote, "src/alpha.txt", "local edit\n")
        review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
        self.production_doc(change_review=review)
        if handoff:
            _ = self.child_production()
        checkpoint = self.dated_checkpoint(
            "checkpoint(alpha): publication records\n\n"
            + "Change-Review: The published change remains recorded | Open the fixture | The change is listed")

        def invoke() -> subprocess.CompletedProcess[str]:
            return self.run_handoff(checkpoint) if handoff else self.run_checkpoint(checkpoint).process

        receipts: list[dict[str, object]] = []
        rows = b""
        for attempt in range(2):
            failed = invoke()
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            if mac_failure:
                self.assertIn("promote", failed.stdout)
            else:
                self.assertIn('promotion refused: tracked edits overlap promotion: "src/alpha.txt"', failed.stdout)
            merge_head = self.git("rev-parse", "HEAD")
            self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge_head)
            receipt, = self.receipts()
            self.assertEqual(receipt["source_checkpoint"], checkpoint)
            self.assertEqual(receipt["merge_commit"], merge_head)
            self.assertEqual(change_review.pending(review), [], "a failed promotion must not add a row")
            notices = self.events("style_sync.py")
            self.assertEqual(len(notices), 1, "promotion retries must not duplicate the publication notice")
            self.assertIn(merge_head, notices[0])
            marker = "handoff-promotion" if handoff else "promotion"
            self.assertFalse(merge_checkpoint.checkpoint_step_record(self.checkout, merge_head, marker).exists())
            self.assertFalse(self.log.exists(), "a failed promotion must not write a completion log")
            if handoff:
                self.assertEqual(self.events("send.py"), [], "a failed promotion must not acknowledge completion")
            if attempt == 0:
                receipts = self.receipts()
                rows = change_review.read(review).encode()
            else:
                self.assertEqual(self.receipts(), receipts)
                self.assertEqual(change_review.read(review).encode(), rows)
        if mac_failure:
            _ = (self.state / "mac-rc").write_text("0\n")
        else:
            _ = self.git("restore", "src/alpha.txt", cwd=self.promote)
        for attempt in range(2):
            retried = invoke()
            self.assertEqual(retried.returncode, 0, f"retry {attempt}: {retried.stdout}{retried.stderr}")
            self.assertEqual(self.receipts(), receipts)
            self.assertEqual(review.read_text().count("The published change remains recorded"), 1)
            if attempt == 0:
                rows = review.read_bytes()
            else:
                self.assertEqual(review.read_bytes(), rows)
            self.assertEqual(len(self.events("style_sync.py")), 1)
        self.assertTrue(self.log.exists())

    def test_local_promotion_failure_keeps_publication_records_without_duplicates(self) -> None:
        self.assert_publication_records_survive_promotion_failure(mac_failure=False, handoff=False)

    def test_mac_pull_failure_keeps_publication_records_without_duplicates(self) -> None:
        self.assert_publication_records_survive_promotion_failure(mac_failure=True, handoff=False)

    def test_handoff_local_promotion_failure_keeps_publication_records_without_duplicates(self) -> None:
        self.assert_publication_records_survive_promotion_failure(mac_failure=False, handoff=True)

    def test_handoff_mac_pull_failure_keeps_publication_records_without_duplicates(self) -> None:
        self.assert_publication_records_survive_promotion_failure(mac_failure=True, handoff=True)

    def test_bump_gate_is_launched_before_local_promotion_and_reused_on_retry(self) -> None:
        class InterruptedPromotion(BaseException):
            pass

        self.prepare_bump_gate()
        gate_command = self.bin / "checkpoint-test"
        self.executable(gate_command, gate_command.read_text() + '''
(state / "detached-lock-marker").write_text(os.environ.get("MAIN_PUSH_LOCK_HELD", ""))
''')
        self.promotions = str(self.promote)
        self.production_doc()
        checkpoint = self.checkpoint()
        records: list[checkpoint_gate.BumpGateRecord] = []
        original_promote = merge_checkpoint.promote

        def promote_after_gate(request: merge_checkpoint.MergeRequest, tip: str, *, local_only: bool = False) -> None:
            self.assertTrue(local_only)
            self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tip)
            merge_head = self.git("rev-parse", "HEAD")
            path = merge_checkpoint.checkpoint_step_record(self.checkout, merge_head, "bump-gate")
            self.assertTrue(path.exists(), "the gate must be recorded before promotion starts")
            record = checkpoint_gate.read_record(path)
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
        self.assertEqual((self.state / "detached-lock-marker").read_text(), "")

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
        original_send = checkpoint_gate.send_result
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
            self.assertEqual(checkpoint_gate.read_record(path)["result"], "red")
            self.assertEqual(len(self.bump_notices()), 0 if launch_failure else 1,
                             "completed gate notices must wait until after local promotion")
            original_promote(request, tip, local_only=local_only)

        def send(path: Path, record: checkpoint_gate.BumpGateRecord) -> None:
            self.assert_main_push_lock_available()
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), record["tip"])
            delivered.append(record["tip"])
            original_send(path, record)

        with mock.patch.object(merge_checkpoint, "start_gate", side_effect=start), \
                mock.patch.object(merge_checkpoint, "promote", side_effect=local), \
                mock.patch.object(checkpoint_gate, "send_result", side_effect=send):
            result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertEqual(delivered, [self.git("rev-parse", "HEAD")])
        gate_path = merge_checkpoint.checkpoint_step_record(self.checkout, self.git("rev-parse", "HEAD"), "bump-gate")
        gate_record = checkpoint_gate.read_record(gate_path)
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

        def zone(mac: PurePosixPath) -> None:
            self.assertEqual(mac, self.root / "mac-installed")
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
        with mock.patch.object(merge_checkpoint, "promote", side_effect=checkpoint_gate.Stop(
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
        self.assertEqual(self.events("checkpoint-test"), [*test_events, []])

    def test_checkpoint_retry_integrates_main_fix_before_finishing_promotion(self) -> None:
        self.assert_interrupted_promotion_keeps_new_main_fix(handoff=False)

    def test_handoff_retry_integrates_main_fix_before_finishing_promotion(self) -> None:
        self.assert_interrupted_promotion_keeps_new_main_fix(handoff=True)

    def test_handoff_retest_blocks_main_push_until_promotion_finishes(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        _ = self.child_production()
        checkpoint = self.checkpoint()
        with mock.patch.object(merge_checkpoint, "promote", side_effect=checkpoint_gate.Stop(
                "promote", "failed", "interrupted promotion", "retry")):
            failed = self.run_handoff(checkpoint)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        test_events = self.events("checkpoint-test")
        fix = self.push_main_change("fast-fix.txt", "keep this main fix\n")
        writer = self.root / "retest-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(writer), cwd=self.root)
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        original_tests = merge_checkpoint.checkpoint_tests
        competing_tips: list[str] = []

        def try_main_push() -> None:
            with (common / "main-push.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                _ = self.git("fetch", "origin", "main", cwd=writer)
                _ = self.git("reset", "--hard", "origin/main", cwd=writer)
                self.write(writer, "during-retest.txt", "the competing main change\n")
                _ = self.git("add", "during-retest.txt", cwd=writer)
                _ = self.git("-c", "user.name=Checkpoint Test", "-c", "user.email=checkpoint@example.invalid",
                             "commit", "-m", "competing main change", cwd=writer)
                _ = self.git("push", "origin", "main", cwd=writer)
                competing_tips.append(self.git("rev-parse", "HEAD", cwd=writer))

        def retest(production: Production, paths: tuple[str, ...], checkpoint: str,
                   merge_tests: tuple[tuple[str, ...], ...], flakes: frozenset[str], scratch: Path,
                   merge_head: str, flaky_tests: frozenset[str], *, before_merge_tip: str = "") -> str:
            result = original_tests(production, paths, checkpoint, merge_tests, flakes, scratch,
                                    merge_head, flaky_tests, before_merge_tip=before_merge_tip)
            if not competing_tips:
                try_main_push()
            return result

        with mock.patch.object(merge_checkpoint, "checkpoint_tests", side_effect=retest):
            resumed = self.run_handoff(checkpoint)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(self.events("checkpoint-test"), [*test_events, []])
        tip = self.git("rev-parse", "main", cwd=self.origin)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tip)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.git("merge-base", "--is-ancestor", fix, tip), "")
        self.assertEqual(self.git("merge-base", "--is-ancestor", checkpoint, tip), "")
        try_main_push()
        competing_tip, = competing_tips
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), competing_tip)
        self.assertEqual(self.git("merge-base", "--is-ancestor", tip, competing_tip, cwd=self.origin), "")

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
        subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True, timeout=__HANG_GUARD__)
    (checkout / "fast-fix.txt").write_text("fast fix after promotion\\n")
    subprocess.run(["git", "add", "fast-fix.txt"], cwd=checkout, check=True, timeout=__HANG_GUARD__)
    subprocess.run(["git", "-c", "user.name=Fast Test", "-c", "user.email=fast@example.invalid",
                    "commit", "-m", "fast fix"], cwd=checkout, check=True, capture_output=True, timeout=__HANG_GUARD__)
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=checkout, check=True, capture_output=True, timeout=__HANG_GUARD__)
'''.replace("__HANG_GUARD__", str(TEST_HANG_GUARD_SECS))
        children: list[subprocess.Popen[str]] = []
        original_promote = merge_checkpoint.promote_local_checkout
        original_push = merge_checkpoint.push_git
        original_tests = merge_checkpoint.tests
        started: list[float] = []
        spans: list[float] = []

        def tests(request: merge_checkpoint.MergeRequest, paths: tuple[str, ...], merge_head: str) -> str:
            self.assert_main_push_lock_available()
            return original_tests(request, paths, merge_head)

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

        with mock.patch.object(merge_checkpoint, "tests", side_effect=tests), \
                mock.patch.object(merge_checkpoint, "push_git", side_effect=push), \
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
        with (mock.patch.dict(os.environ, self.env), production_paths(self.env),
              mock.patch.object(pane_record, "RECORD_DIR", self.root / "panes")):
            request = request_from(argparse.Namespace(
                production="example", unit="alpha", phase="2", hash=sha,
                also=[], shrink=False, as_built=False, trailer=[], review_trial=TRIAL, delivers=DELIVERS,
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
        def now(zone: tzinfo | None = None) -> datetime:
            return MERGED_AT.astimezone(zone)

        with mock.patch("scripts.production.merge_checkpoint.datetime", wraps=datetime, now=now):
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
        self.assertRegex(result.process.stdout, re.escape(f"into: {self.checkout} (production) — ") + ".*unblocks: the unit’s next checkpoint")
        self.assertRegex(result.process.stdout, re.escape(f"into: {self.promote} (main) — ") + ".*unblocks: the live gate of alpha phase 2")
        self.assertIn("the merge branch collects every unit's checkpoints", result.process.stdout)
        self.assertIn("sessions on", result.process.stdout)
        self.assertIn("into: " + str(self.root / "mac-installed"), result.process.stdout)
        self.assertEqual(len(self.events("ssh")), 1)
        review = self.events("review_regime.py")
        self.assertEqual(len(review), 1)
        merged_at = review[0][review[0].index("--merged") + 1]
        self.assertRegex(merged_at, r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(review[0], [
            "add", "--unit", "alpha", "--plan", "docs/plans/alpha.md", "--phase", "2", "--regime", "trial",
            "--started", STARTED, "--merged", merged_at, "--holds", "2",
            "--merge-defects", "1", "--ux-findings", "2", "--code-findings", "3",
            "--review-minutes", "14", "--ux-check-minutes", "4",
            "--ux-repair-minutes", "1", "--excluded", "no merge design check",
        ])
        self.assertRegex(self.log.read_text(), rf"- {MERGED_AT:%H:%M %Z}: alpha phase 2 \([0-9a-f]+\) merged as [0-9a-f]+; .*green; pushed; promoted")
        self.assertIn("send alpha:", result.process.stdout)
    def test_promotion_refusal_keeps_pushed_merge_and_resume_reaches_only_missing_place(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        self.write(self.promote, "src/beta.txt", "staged local edit\n")
        _ = self.git("add", "src/beta.txt", cwd=self.promote)
        self.write(self.promote, "src/beta.txt", "local edit\n")
        self.write(self.promote, "src/alpha.txt", "local edit\n")
        failed = self.run_checkpoint(sha)
        self.assertIn("failed", failed.process.stdout)
        self.assertIn('promotion refused: tracked edits overlap promotion: "src/alpha.txt"', failed.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertEqual(self.git("rev-parse", "HEAD"), pushed)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), failed.before)
        self.assertIn(f"into: {self.checkout} (production)", failed.process.stdout)
        self.assertIn("unblocks: the unit’s next checkpoint", failed.process.stdout)
        self.assertNotIn(f"into: {self.promote} (main)", failed.process.stdout)
        _ = self.git("restore", "src/alpha.txt", cwd=self.promote)
        resumed = self.run_checkpoint(sha)
        self.assertEqual(resumed.process.returncode, 0, resumed.process.stderr + resumed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), pushed)
        self.assertIn(f"into: {self.promote} (main)", resumed.process.stdout)
        self.assertEqual((self.promote / "src/beta.txt").read_text(), "local edit\n")
        self.assertEqual(self.git("show", ":src/beta.txt", cwd=self.promote), "staged local edit")
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
        with self.subTest(mac="unreachable"):
            self.env["ZONE_STATE"] = str(self.root / "zone")
            self.executable(self.bin / "ssh", STUB.replace('if name == "ssh":\n',
                'if name == "ssh":\n'
                + '    print("connection timed out", file=sys.stderr)\n'
                + '    raise SystemExit(255)\n'))
            with mock.patch.object(merge_checkpoint, "send_queued_zone") as send_zone:
                completed = self.run_checkpoint(sha)
            self.assertEqual(completed.process.returncode, 0, completed.process.stdout + completed.process.stderr)
            self.assertIn("promote: ok — mac unreachable, pull pending", completed.process.stdout)
            self.assertNotIn("into: " + str(self.root / "mac-installed"), completed.process.stdout)
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), self.git("rev-parse", "HEAD"))
            self.assertIn("merged as", self.log.read_text())
            send_zone.assert_not_called()
            with mock.patch.dict(os.environ, self.env):
                pending = main_publish.read_queue()
            self.assertIsInstance(pending, main_publish.PendingMacPull)
            assert isinstance(pending, main_publish.PendingMacPull)
            self.assertEqual(pending.text, "")
            self.assertIn("connection timed out", pending.reason)

    def test_mac_pull_retries_once_then_succeeds(self) -> None:
        config = self.root / "merge_checkpoint.conf"
        _ = config.write_text("# whole: 1..\nMAC_PULL_TRIES=3\n# range: 0..\nMAC_PULL_RETRY_SECONDS=0\n")
        self.env["CONF_DIR"] = str(Path(config).parent)
        self.promotions = str(self.promote) + ", mac " + str(self.root / "mac-installed")
        self.production_doc()
        self.executable(self.bin / "ssh", STUB.replace('if name == "ssh":\n',
            'if name == "ssh":\n'
            + '    calls = [json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()]\n'
            + '    rc = "128" if sum(row["command"] == "ssh" for row in calls) == 1 else "0"\n'
            + '    (state / "mac-rc").write_text(rc)\n'))
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        calls = self.events("ssh")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertIn(f"promote: ok — mac {self.root / 'mac-installed'} main pulled", result.process.stdout)

    def test_mac_pull_stops_after_configured_tries_with_the_last_failure(self) -> None:
        config = self.root / "merge_checkpoint.conf"
        _ = config.write_text("# whole: 1..\nMAC_PULL_TRIES=2\n# range: 0..\nMAC_PULL_RETRY_SECONDS=0\n")
        self.env["CONF_DIR"] = str(Path(config).parent)
        self.promotions = str(self.promote) + ", mac " + str(self.root / "mac-installed")
        self.production_doc()
        _ = (self.state / "mac-rc").write_text("128\n")
        self.executable(self.bin / "ssh", STUB.replace('if name == "ssh":\n',
            'if name == "ssh":\n'
            + '    calls = [json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()]\n'
            + '    count = sum(row["command"] == "ssh" for row in calls)\n'
            + '    print("fatal: diverged checkout, attempt " + str(count), file=sys.stderr)\n'))
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 1)
        calls = self.events("ssh")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertIn("promote: failed", result.process.stdout)
        self.assertIn("fatal: diverged checkout, attempt 2", result.process.stdout)
        self.assertNotIn("fatal: diverged checkout, attempt 1", result.process.stdout)

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
        self.assertIn("unblocks: the unit’s next checkpoint", resumed.process.stdout)
    def test_a_merge_killed_before_its_push_is_tested_before_a_rerun_pushes_it(self) -> None:
        # Killed after the merge and before its tests finished: nothing proves the merge green.
        sha = self.checkpoint()
        before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        subject = f"Merge alpha phase 2 ({sha[:7]}) into production"
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
