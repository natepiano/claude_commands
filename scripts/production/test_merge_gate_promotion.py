"""Merge gate promotion contracts."""

from __future__ import annotations

import unittest

import json
import io
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import final
from contextlib import redirect_stdout
from unittest import mock
from scripts.production import merge_gate
from scripts.production.merge_gate_test_support import MergeGateFixture, TEST_HANG_GUARD_SECS, wait_for_file


@final
class MergeGatePromotionTests(MergeGateFixture):
    def test_mac_timeout_or_interruption_reports_that_main_was_already_promoted(self) -> None:
        args = merge_gate.GateArguments(main_repo=str(self.main), merge_branch="integration", settings_blob=self.settings_blob,
                                       ssh="fake-ssh", mac_repo="~/fixture", mac_host="fixture")
        for failure in (subprocess.TimeoutExpired("fake-ssh", 25), merge_gate.GateRefused("interrupted by SIGTERM"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), mock.patch.dict(os.environ, self.environment), mock.patch.object(merge_gate, "wait_for_release_lock"), mock.patch.object(
                    merge_gate, "checked_git", side_effect=["main", "", self.base, self.base, self.settings_blob, "", "", self.base]) as checked, mock.patch.object(
                    merge_gate, "git", return_value=subprocess.CompletedProcess([], 0, "", "")), mock.patch.object(
                    subprocess, "run", side_effect=failure):
                with self.assertRaisesRegex(merge_gate.GateRefused, f"main promoted to {self.base}; Mac fast-forward failed"):
                    merge_gate.promote(args)
            self.assertIn(mock.call(self.main, "push", "origin", "main"), checked.call_args_list)

    def test_settings_filter_rejects_changed_blob(self) -> None:
        self.write(self.unit / "settings.json", '{"unexpected": true}\n')
        self.checkpoint = self.commit(self.unit, "unapproved settings")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("settings", (result.stdout + result.stderr).lower())
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)

    def test_promote_fast_forwards_scratch_main_and_checks_mac_result(self) -> None:
        result = self.run_gate("--promote")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), head)
        ssh_calls = [call for call in self.calls() if call["tool"] == "ssh"]
        self.assertEqual(len(ssh_calls), 1)
        self.assertIn("--ff-only", " ".join(ssh_calls[0]["args"]))
        self.assertIn("ff_rc=", " ".join(ssh_calls[0]["args"]))

    def test_promote_only_promotes_the_pushed_merge_branch_without_a_gate(self) -> None:
        self.assertEqual(self.run_gate("--promote").returncode, 0)
        result = self.run_promote_only()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "main"))
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 2)

    def test_finished_release_and_live_command_text_do_not_block_promotion(self) -> None:
        args = self.publish_for_promotion()
        release = Path(self.environment["BUILD_HOLD_RELEASE_DIR"])
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        cycle_id = "a" * 32
        instant = "2026-10-28T14:50:00-04:00"
        self.write(release / cycle_id / "cycle.json", json.dumps({
            "id": cycle_id, "opened_at": instant, "release_started_at": instant,
            "holders": {"fixture": {"since": instant, "released_at": instant}},
            "recipients": {}, "entries": []}))
        self.write(release / "current", cycle_id)
        self.write(release / "release.lock", "")
        ready = self.state / "command-text-ready"
        source = ("import pathlib, signal, sys; signal.signal(signal.SIGTERM, signal.SIG_DFL); "
                  "pathlib.Path(sys.argv[1]).touch(); sys.stdin.readline()")
        process = subprocess.Popen(
            [sys.executable, "-c", source, str(ready), "build_hold.py", "release"],
            env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
        try:
            wait_for_file(ready)
            with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
                merge_gate.promote(args)
            self.assertIsNone(process.poll())
            self.assertEqual(list(holders.iterdir()), [])
            self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
            self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_promotion_waits_for_another_process_to_free_the_release_lock(self) -> None:
        args = self.publish_for_promotion()
        with self.release_lock_held_by_child() as process:
            def release_during_wait(_seconds: float) -> None:
                self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
                self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))
                _, errors = process.communicate(input="release\n", timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 0, errors)

            pause = mock.Mock(side_effect=release_during_wait)
            clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[10.0, 10.0, 11.0]), sleep=pause)
            with mock.patch.dict(os.environ, self.environment), \
                    mock.patch.object(merge_gate, "time", clock), redirect_stdout(io.StringIO()):
                merge_gate.promote(args, release_lock_wait_s=0.5, release_lock_poll_s=0.01)
            pause.assert_called_once_with(0.01)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_promotion_refuses_when_the_release_lock_stays_held_for_the_whole_wait(self) -> None:
        args = self.publish_for_promotion()
        pause = mock.Mock()
        clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[10.0, 10.0, 70.0]), sleep=pause)
        with self.release_lock_held_by_child(), mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "time", clock):
            with self.assertRaisesRegex(merge_gate.GateRefused,
                    "^build-hold state stayed locked for 60 s; main promotion refused$"):
                merge_gate.promote(args)
            pause.assert_called_once_with(0.2)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promotion_does_not_create_a_missing_release_directory_or_lock(self) -> None:
        args = self.publish_for_promotion()
        release = Path(self.environment["BUILD_HOLD_RELEASE_DIR"])
        with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
            merge_gate.promote(args)
            self.assertFalse(release.exists())
            release.mkdir()
            merge_gate.promote(args)
            self.assertEqual(list(release.iterdir()), [])
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 2)

    def test_promote_refuses_unrelated_local_edits_and_keeps_untracked_files(self) -> None:
        _ = self.publish_for_promotion()
        self.write(self.main / "subject.txt", "uncommitted main\n")
        self.write(self.main / "settings.json", "local settings\n")
        self.write(self.main / "accounts.lock", "untracked\n")
        result = self.run_promote_only()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"subject.txt"', result.stderr)
        self.assertIn('"settings.json"', result.stderr)
        self.assertEqual((self.main / "subject.txt").read_text(), "uncommitted main\n")
        self.assertEqual((self.main / "accounts.lock").read_text(), "untracked\n")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promotion_refuses_staged_edits_even_when_the_file_matches_head(self) -> None:
        _ = self.publish_for_promotion()
        self.write(self.main / "subject.txt", "staged main\n")
        _ = self.git(self.main, "add", "subject.txt")
        self.write(self.main / "subject.txt", "base\n")
        result = self.run_promote_only()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"subject.txt"', result.stderr)
        self.assertEqual(self.git(self.main, "show", ":subject.txt"), "staged main")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_promotion_refuses_edits_hidden_by_index_flags(self) -> None:
        self.write(self.unit / "subject.txt", "promoted contents\n")
        self.checkpoint = self.commit(self.unit, "update subject")
        _ = self.publish_for_promotion()
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag=flag):
                _ = self.git(self.main, "update-index", "--" + flag, "subject.txt")
                self.write(self.main / "subject.txt", "live contents\n")
                self.assertEqual(self.git(self.main, "status", "--porcelain", "--untracked-files=no"), "")
                result = self.run_promote_only()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('"subject.txt"', result.stderr)
                self.assertEqual((self.main / "subject.txt").read_text(), "live contents\n")
                self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
                _ = self.git(self.main, "update-index", "--no-" + flag, "subject.txt")

    def test_promotion_checks_edits_after_the_last_hold_wait(self) -> None:
        args = self.publish_for_promotion()
        waits = 0

        def edit_during_wait() -> None:
            nonlocal waits
            waits += 1
            if waits == 2:
                self.write(self.main / "subject.txt", "edited during hold\n")

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate.BuildHoldWait, "wait_until_free", side_effect=edit_during_wait):
            with self.assertRaisesRegex(merge_gate.GateRefused, 'tracked edits differ from HEAD: "subject.txt"'):
                merge_gate.promote(args)
        self.assertEqual((self.main / "subject.txt").read_text(), "edited during hold\n")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promotion_allows_untracked_files(self) -> None:
        _ = self.publish_for_promotion()
        self.write(self.main / "accounts.lock", "untracked\n")
        result = self.run_promote_only()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.main / "accounts.lock").read_text(), "untracked\n")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_mac_promotion_checks_real_files_and_index_in_a_private_clone(self) -> None:
        args = self.publish_for_promotion()
        self.write(self.bin / "ssh", f'#!{sys.executable}\nimport subprocess, sys\n'
                   + 'raise SystemExit(subprocess.call(["sh", "-c", sys.argv[-1]]))\n')
        for state in ("modified", "staged", "assume-unchanged", "skip-worktree", "untracked", "clean"):
            with self.subTest(state=state):
                mac = self.root / ("mac-" + state)
                _ = self.git(self.root, "clone", "-q", "--branch", "main", str(self.origin), str(mac))
                # Keep each Mac at the old promotion, even after natedev advances.
                _ = self.git(mac, "reset", "--hard", self.base)
                args.mac_repo = str(mac)
                if state in ("assume-unchanged", "skip-worktree"):
                    _ = self.git(mac, "update-index", "--" + state, "subject.txt")
                if state not in ("untracked", "clean"):
                    self.write(mac / "subject.txt", "Mac local contents\n")
                if state == "staged":
                    _ = self.git(mac, "add", "subject.txt")
                if state == "untracked":
                    self.write(mac / "local.lock", "keep\n")
                with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
                    if state in ("untracked", "clean"):
                        merge_gate.promote(args)
                        self.assertEqual(self.git(mac, "rev-parse", "HEAD"), self.checkpoint)
                    else:
                        with self.assertRaisesRegex(merge_gate.GateRefused, 'Mac fast-forward failed:.*"subject.txt"'):
                            merge_gate.promote(args)
                        self.assertEqual(self.git(mac, "rev-parse", "HEAD"), self.base)
                        self.assertEqual((mac / "subject.txt").read_text(), "Mac local contents\n")
                self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)
                if state == "untracked":
                    self.assertEqual((mac / "local.lock").read_text(), "keep\n")

    def test_promote_preserves_local_edits_that_block_fast_forward(self) -> None:
        self.write(self.unit / "subject.txt", "checkpoint changes this file\n")
        self.checkpoint = self.commit(self.unit, "touch subject")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        self.write(self.main / "subject.txt", "uncommitted main\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.main / "subject.txt").read_text(), "uncommitted main\n")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promote_refuses_divergent_main(self) -> None:
        self.write(self.main / "main-only.txt", "divergent\n")
        main_head = self.commit(self.main, "main divergence")
        _ = self.git(self.main, "push", "origin", "main")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), main_head)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), main_head)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promote_refuses_unpublished_main_commits(self) -> None:
        self.write(self.main / "main-only.txt", "unpublished\n")
        main_head = self.commit(self.main, "unpublished main")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), main_head)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promote_refuses_checkout_on_another_branch(self) -> None:
        _ = self.git(self.main, "checkout", "-b", "other-main")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "branch", "--show-current"), "other-main")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_mac_nonzero_ff_rc_is_failure_even_when_ssh_exits_zero(self) -> None:
        self.write(self.state / "mac-answer", "ff_rc=1\nfixture-head\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ff_rc=1", result.stdout + result.stderr)
        head = self.git(self.origin, "rev-parse", "main")
        self.assertIn(f"main promoted to {head}; Mac fast-forward failed", result.stderr)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)

    def test_mac_missing_ff_rc_is_failure(self) -> None:
        self.write(self.state / "mac-answer", "fixture-head\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)

    def test_mac_conflicting_ff_rc_lines_are_failure(self) -> None:
        self.write(self.state / "mac-answer", "ff_rc=0\nff_rc=1\nfixture-head\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)


if __name__ == "__main__":
    _ = unittest.main()
