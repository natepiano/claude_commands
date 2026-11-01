"""A user's bump pushes now and reports the gate run on that pushed tip."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast, final
from unittest import mock
from collections.abc import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_checkpoint
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.message import sessions
from scripts.production.merge_checkpoint_test_support import (
    BumpGateObservation, MergeCheckpointFixture, STUB, configure_test_environment,
)


def setUpModule() -> None:
    configure_test_environment()


class InterruptedBeforePush(BaseException):
    """Keep the real unpushed merge on disk, as a stopped command would."""


@final
class CheckpointBumpTests(MergeCheckpointFixture):
    def assert_pushed(self, checkpoint: str) -> str:
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tip)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), checkpoint)
        self.assertEqual(self.git("status", "--porcelain"), "")
        return tip

    def notice_text(self, notice: list[str]) -> str:
        return notice[notice.index("--text") + 1]

    def notice_recipient(self, notice: list[str]) -> str:
        address = notice[notice.index("--to") + 1]
        for path in (self.root / "sessions").glob("*.json"):
            row = cast(dict[str, object], json.loads(path.read_text()))
            session_id, socket = cast(str, row["sessionId"]), cast(str, row["messagingSocketPath"])
            if address in ("session:" + session_id, "uds:" + socket):
                return session_id.removeprefix("bump-")
        self.fail(f"notification did not address a fixture recipient: {address}")

    def notice_log(self, text: str) -> Path:
        match = re.search(r"/[^\s;,]+\.log", text)
        self.assertIsNotNone(match, text)
        assert match is not None
        path = Path(match[0])
        self.assertTrue(path.is_file(), text)
        return path

    def finish_gate(self, run: BumpGateObservation) -> None:
        self.release_bump_gate()
        self.wait_bump_gate_exit(run)
        self.assertFalse(run.checkout.exists(), "the gate must remove its clone after the run")

    def test_red_gate_reports_both_recipients_after_the_merge_is_pushed(self) -> None:
        self.prepare_bump_gate(red=True)
        self.promotions = str(self.promote)
        self.production_doc()
        checkpoint = self.checkpoint()
        with LauncherHangGuard("bumped merge returns before its gate").protect():
            result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        tip = self.assert_pushed(checkpoint)
        self.assertIn("pushed tip", result.process.stdout)
        self.assertRegex(self.log.read_text(), r"gate[^\n]*(?:after (?:the )?push|pushed tip)")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        run = self.wait_bump_gate_runs(1)[0]
        self.assertEqual(run.origin, tip, "the gate must begin after the origin push")
        self.finish_gate(run)
        notices = self.bump_notices()
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual({self.notice_recipient(notice) for notice in notices}, {"alpha", "showrunner"})
        for notice in notices:
            text = self.notice_text(notice)
            self.assertIn("merge test 1", text)
            self.assertIn("bump_contract_failed", text)
            self.assertIn(tip, text)
            log = self.notice_log(text)
            self.assertIn("bump_contract_failed", log.read_text())
            self.assertFalse(log.is_relative_to(run.checkout))
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_green_gate_uses_a_detached_clone_and_notifies_only_the_showrunner(self) -> None:
        self.prepare_bump_gate()
        checkpoint = self.checkpoint()
        with LauncherHangGuard("bumped merge returns before its gate").protect():
            result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        tip = self.assert_pushed(checkpoint)
        run = self.wait_bump_gate_runs(1)[0]
        self.assertEqual((run.head, run.origin), (tip, tip))
        self.assertNotEqual(run.checkout, self.checkout)
        self.assertTrue((run.checkout / ".git").is_dir(), "the gate needs a clone, not a worktree pointer or export")
        alternates = run.checkout / ".git/objects/info/alternates"
        self.assertEqual(alternates.read_text().strip(), self.git("rev-parse", "--path-format=absolute", "--git-path", "objects"))
        self.assertEqual(run.branch, "", "the clone must have a detached HEAD")
        self.assertEqual(run.session, run.gate_pid, "the gate must run in its own session")
        self.assertTrue(run.stdin_closed)
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.finish_gate(run)
        notices = self.bump_notices()
        self.assertEqual(len(notices), 1, notices)
        self.assertEqual(self.notice_recipient(notices[0]), "showrunner")
        self.assertEqual(len(self.notice_text(notices[0]).splitlines()), 1)
        self.assertIn("green", self.notice_text(notices[0]).lower())
        self.assertIn(tip, self.notice_text(notices[0]))
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_bump_refuses_shrink_as_built_and_child_handoff_before_merging(self) -> None:
        self.prepare_bump_gate(blocked=False)
        checkpoint = self.checkpoint()
        before = self.git("rev-parse", "HEAD")
        for option in ("--shrink", "--as-built", "--from-child"):
            with self.subTest(option=option):
                result = (self.run_handoff(checkpoint, "--bump") if option == "--from-child"
                          else self.run_checkpoint(checkpoint, option, "--bump", review=False).process)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("--bump", result.stdout + result.stderr)
                self.assertEqual(self.git("rev-parse", "HEAD"), before)
                self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), before)
                self.assertEqual(self.bump_gate_runs(), [])
                self.assertEqual(self.events("checkpoint-test"), [])

    def test_live_gate_is_not_started_twice_for_the_same_tip(self) -> None:
        self.prepare_bump_gate()
        checkpoint = self.checkpoint()
        with LauncherHangGuard("initial bumped merge").protect():
            first = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(first.process.returncode, 0, first.process.stdout + first.process.stderr)
        run = self.wait_bump_gate_runs(1)[0]
        tip = self.assert_pushed(checkpoint)
        record = self.bump_gate_record(tip)
        self.assertEqual(record["result"], "pending")
        self.assertEqual((record["pid"], record["proc_start"]), (run.gate_pid, run.gate_start))
        self.assertEqual(record["tip"], tip)
        self.assertTrue(record["started"])
        with LauncherHangGuard("same-tip bumped rerun").protect():
            repeated = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(repeated.process.returncode, 0, repeated.process.stdout + repeated.process.stderr)
        self.assertEqual(self.bump_gate_runs(), [run])
        self.assertEqual(self.bump_gate_record(tip), record)
        self.finish_gate(run)
        self.assertEqual(self.bump_gate_record(tip)["result"], "green")
        self.assertEqual(self.bump_gate_runs(), [run])
        self.assertEqual(len(self.bump_notices()), 1)
        completed = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(completed.process.returncode, 0, completed.process.stdout + completed.process.stderr)
        self.assertEqual(self.bump_gate_runs(), [run])
        self.assertEqual(len(self.bump_notices()), 1)

    def test_rerun_restarts_a_gate_that_died_without_a_result(self) -> None:
        self.prepare_bump_gate()
        checkpoint = self.checkpoint()
        with LauncherHangGuard("initial bumped merge").protect():
            first = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(first.process.returncode, 0, first.process.stdout + first.process.stderr)
        dead = self.wait_bump_gate_runs(1)[0]
        self.assertEqual(self.bump_gate_record(dead.head)["result"], "pending")
        self.stop_bump_gate(dead)
        self.assertEqual(self.bump_notices(), [])
        with LauncherHangGuard("restart a stopped background gate").protect():
            restarted = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(restarted.process.returncode, 0, restarted.process.stdout + restarted.process.stderr)
        runs = self.wait_bump_gate_runs(2)
        live = next(run for run in runs if run != dead)
        record = self.bump_gate_record(live.head)
        self.assertEqual(record["result"], "pending")
        self.assertEqual((record["pid"], record["proc_start"]), (live.gate_pid, live.gate_start))
        self.assertNotEqual(live.gate_pid, dead.gate_pid)
        self.assertEqual(live.head, dead.head)
        self.finish_gate(live)
        self.assertEqual(len(self.bump_notices()), 1)
        self.assertEqual(self.notice_recipient(self.bump_notices()[0]), "showrunner")

    def test_bump_resumes_an_unpushed_merge_without_running_tests_first(self) -> None:
        self.prepare_bump_gate()
        checkpoint = self.checkpoint()
        before = self.git("rev-parse", "production", cwd=self.origin)
        with mock.patch.object(merge_checkpoint, "push_git", side_effect=InterruptedBeforePush):
            with self.assertRaises(InterruptedBeforePush), LauncherHangGuard("bump stops before push").protect():
                _ = self.run_checkpoint(checkpoint, "--bump")
        merge = self.git("rev-parse", "HEAD")
        self.assertNotEqual(merge, before)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), before)
        self.assertEqual(self.events("checkpoint-test"), [])
        with LauncherHangGuard("resume bumped merge without pre-push tests").protect():
            resumed = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(resumed.process.returncode, 0, resumed.process.stdout + resumed.process.stderr)
        self.assertEqual(self.assert_pushed(checkpoint), merge)
        self.finish_gate(self.wait_bump_gate_runs(1)[0])
        self.assertEqual(len(self.bump_gate_runs()), 1)

    def test_failed_send_is_logged_and_retried_once(self) -> None:
        self.prepare_bump_gate(red=True)
        (self.state / "send-fail-once").touch()
        checkpoint = self.checkpoint()
        with LauncherHangGuard("bump with a failed notification").protect():
            result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        run = self.wait_bump_gate_runs(1)[0]
        self.finish_gate(run)
        notices = self.bump_notices()
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual(len(self.events("send.py")), 3)
        self.assertEqual({self.notice_recipient(notice) for notice in notices}, {"alpha", "showrunner"})
        log = self.notice_log(self.notice_text(notices[0]))
        self.assertIn("fixture send failed", log.read_text())

    def test_clone_failure_before_the_first_step_is_red_for_both_recipients(self) -> None:
        self.prepare_bump_gate()
        self.refuse_bump_clone()
        checkpoint = self.checkpoint()
        result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        tip = self.assert_pushed(checkpoint)
        record = self.wait_bump_result(tip)
        self.assertEqual(record["result"], "red")
        self.assertEqual(self.bump_gate_runs(), [])
        self.assertEqual(self.events("checkpoint-test"), [])
        notices = self.bump_notices()
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual({self.notice_recipient(notice) for notice in notices}, {"alpha", "showrunner"})
        for notice in notices:
            text = self.notice_text(notice)
            self.assertIn(tip, text)
            self.assertIn("clone", text)
            self.assertIn("fixture clone refused", self.notice_log(text).read_text())

    def test_launcher_failure_is_red_and_does_not_stop_promotion(self) -> None:
        self.prepare_bump_gate()
        self.promotions = str(self.promote)
        self.production_doc()
        checkpoint = self.checkpoint()
        real_process = mock.Mock(wraps=subprocess.Popen)

        def launch(command: Sequence[str], *args: object, **kwargs: object) -> subprocess.Popen[str] | subprocess.Popen[bytes]:
            if any(Path(word).name == "bump_gate.py" for word in command):
                raise OSError("fixture gate launch refused")
            return cast(subprocess.Popen[str] | subprocess.Popen[bytes], real_process(command, *args, **kwargs))

        with mock.patch.object(subprocess, "Popen", side_effect=launch):
            result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        tip = self.assert_pushed(checkpoint)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.bump_gate_record(tip)["result"], "red")
        self.assertEqual(self.bump_gate_runs(), [])
        notices = self.bump_notices()
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual({self.notice_recipient(notice) for notice in notices}, {"alpha", "showrunner"})
        for notice in notices:
            text = self.notice_text(notice)
            self.assertIn(tip, text)
            self.assertIn("fixture gate launch refused", text)
            self.assertIn("fixture gate launch refused", self.notice_log(text).read_text())

    def test_first_step_that_cannot_start_is_red_after_push(self) -> None:
        self.prepare_bump_gate()
        checkpoint = self.checkpoint()
        notify_style = merge_checkpoint.notify_style

        def remove_gate_command(checkout: Path, branch: str, tip: str) -> None:
            notify_style(checkout, branch, tip)
            (self.bin / "checkpoint-test").unlink()

        with mock.patch.object(merge_checkpoint, "notify_style", side_effect=remove_gate_command):
            result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        tip = self.assert_pushed(checkpoint)
        record = self.wait_bump_result(tip)
        self.assertEqual(record["result"], "red")
        self.assertEqual(self.bump_gate_runs(), [])
        notices = self.bump_notices()
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual({self.notice_recipient(notice) for notice in notices}, {"alpha", "showrunner"})
        for notice in notices:
            text = self.notice_text(notice)
            self.assertIn("merge test 1", text)
            self.assertIn(tip, text)
            self.assertIn("checkpoint-test", self.notice_log(text).read_text())

    def test_known_named_flake_reruns_alone_and_type_footer_passes(self) -> None:
        self.prepare_bump_gate(red=True)
        self.known_flaky_tests = "`bump_contract_failed`"
        self.executable(self.bin / "basedpyright", STUB)
        self.production_doc(merge_tests_line="- **Merge tests:** `checkpoint-test`, `basedpyright scripts`")
        checkpoint = self.checkpoint()
        with LauncherHangGuard("bump with a named flake and type check").protect():
            result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        run = self.wait_bump_gate_runs(1)[0]
        self.finish_gate(run)
        self.assertEqual(self.bump_gate_record(run.head)["result"], "green")
        self.assertEqual(self.events("checkpoint-test"), [[], ["-E", "test(=bump_contract_failed)"]])
        self.assertEqual(self.events("basedpyright"), [["scripts"]])
        notices = self.bump_notices()
        self.assertEqual(len(notices), 1, notices)
        self.assertEqual(self.notice_recipient(notices[0]), "showrunner")

    def test_without_bump_the_same_red_gate_stops_the_merge(self) -> None:
        self.prepare_bump_gate(red=True, blocked=False)
        checkpoint = self.checkpoint()
        result = self.run_checkpoint(checkpoint)
        self.assertNotEqual(result.process.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), result.before)
        self.assertIn("merge test 1", result.process.stdout)
        self.assertEqual(self.bump_notices(), [])

    def test_gate_notices_follow_recipient_moves_during_the_run(self) -> None:
        self.prepare_bump_gate(red=True)
        checkpoint = self.checkpoint()
        result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        run = self.wait_bump_gate_runs(1)[0]
        self.move_bump_recipients()
        self.finish_gate(run)
        notices = self.bump_notices()
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual({self.notice_recipient(notice) for notice in notices},
                         {"alpha-moved", "showrunner-moved"})

    def test_completed_gate_rerun_delivers_only_the_unsent_recipient(self) -> None:
        self.prepare_bump_gate(red=True)
        marker = self.state / "stop-before-showrunner-notice"
        marker.touch()
        checkpoint = self.checkpoint()
        first = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(first.process.returncode, 0, first.process.stdout + first.process.stderr)
        run = self.wait_bump_gate_runs(1)[0]
        self.release_bump_gate()
        self.wait_bump_gate_exit(run)
        self.assertEqual(self.bump_gate_record(run.head)["result"], "red")
        notices = self.bump_notices()
        self.assertEqual([self.notice_recipient(notice) for notice in notices], ["alpha"])
        runs_before_delivery = self.bump_gate_runs()
        marker.unlink()
        repeated = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(repeated.process.returncode, 0, repeated.process.stdout + repeated.process.stderr)
        notices = self.bump_notices()
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual(sorted(self.notice_recipient(notice) for notice in notices), ["alpha", "showrunner"])
        self.assertEqual(self.bump_gate_runs(), runs_before_delivery, "delivery recovery must not rerun tests")

    def test_superseded_runner_cannot_replace_the_new_owner_or_send_a_result(self) -> None:
        self.prepare_bump_gate()
        checkpoint = self.checkpoint()
        result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        run = self.wait_bump_gate_runs(1)[0]
        replacement = self.bump_gate_record(run.head)
        replacement.update(tip=self.git("rev-parse", "HEAD^1"), pid=os.getpid(),
                           proc_start=sessions.process_start(os.getpid()))
        path = merge_checkpoint.checkpoint_step_record(self.checkout, run.head, "bump-gate")
        pending = path.with_suffix(".replacement")
        _ = pending.write_text(json.dumps(replacement))
        _ = pending.replace(path)
        self.finish_gate(run)
        self.assertEqual(self.bump_gate_record(run.head), replacement)
        self.assertEqual(self.bump_notices(), [])
        self.assertIn("superseded", Path(cast(str, replacement["log"])).read_text().lower())

    def test_second_runner_cannot_claim_a_record_held_by_a_live_gate(self) -> None:
        self.prepare_bump_gate()
        checkpoint = self.checkpoint()
        result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        run = self.wait_bump_gate_runs(1)[0]
        record = self.bump_gate_record(run.head)
        competing = self.launch_competing_bump_gate(run.head)
        self.wait_competing_bump_gate(competing)
        self.finish_gate(run)
        self.assertEqual(competing.wait(timeout=TEST_HANG_GUARD_SECS), 0)
        self.assertEqual(self.bump_gate_runs(), [run])
        final_record = self.bump_gate_record(run.head)
        self.assertEqual((final_record["pid"], final_record["proc_start"]),
                         (record["pid"], record["proc_start"]))
        self.assertEqual(len(self.bump_notices()), 1)

    def test_known_flake_note_survives_clone_cleanup_in_the_production_log(self) -> None:
        self.prepare_bump_gate()
        (self.state / "gate-fail-once").touch()
        self.known_flakes = "`merge test 1`"
        self.production_doc()
        checkpoint = self.checkpoint()
        result = self.run_checkpoint(checkpoint, "--bump")
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        run = self.wait_bump_gate_runs(1)[0]
        self.finish_gate(run)
        self.assertEqual(self.bump_gate_record(run.head)["result"], "green")
        self.assertIn("known flake merge test 1: red in batch, green alone", self.log.read_text())


if __name__ == "__main__":
    _ = unittest.main()
