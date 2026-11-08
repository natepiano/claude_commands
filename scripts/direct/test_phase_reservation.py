#!/usr/bin/env python3
"""Tests for the phase reservation state verbs: enroll, committed, clear, and restore on reopen."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Event, current_thread
from typing import cast, final, override
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.direct import phase_reservation, plan_history  # noqa: E402
from scripts.agents import worker_lifecycle
from scripts.lib import atomic_file
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_unit_location, isolated_staff_file
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS

ACTIVE: dict[str, object] = {"kind": "active", "active_phase_reservation": {
    "reservation_id": "r1", "coordination_run_id": "c1", "phase": "34h", "phase_start_head": "a" * 40}}


def record_unit(test: unittest.TestCase, run: Path, home: Path, phase: str) -> None:
    """Record a private unit and its durable run owner."""
    _ = test.enterContext(isolated_staff_file(run / "staff.json", {}))
    _ = (run / "staff.json").write_text(json.dumps({"schema": 2, "rows": []}))
    owner = fixture_member("showrunner", name="reservation")
    row = fixture_unit_location(owner, "reservation", home)
    _ = (run / plan_history.STATE_FILENAME).write_text(json.dumps({
        "owner_staff_id": row.id, "phase": {"id": phase, "status": "active"}, "open_phases": []}))


@final
class WorktreeAttributionTests(unittest.TestCase):
    session: Path = Path()
    first: Path = Path()
    second: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.session = root / "run"
        self.session.mkdir()
        self.first = root / "worktree-x"
        self.second = root / "worktree-y"
        self.first.mkdir()
        record_unit(self, self.session, self.first, "1")
        for command in (
            ["init", "-q", "-b", "main"],
            ["-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-q", "--allow-empty", "-m", "base"],
            ["worktree", "add", "-q", "-b", "other", str(self.second)],
        ):
            _ = subprocess.run(["git", "-C", str(self.first), *command], check=True, capture_output=True,
                               timeout=TEST_HANG_GUARD_SECS)

    def phases(self, worktrees: dict[str, Path]) -> list[phase_reservation.OpenPhase]:
        records = [{"id": name, "instance_id": name, "status": "active"} for name in ("1", "2")]
        state = cast(dict[str, object], json.loads((self.session / plan_history.STATE_FILENAME).read_text()))
        state.update({
            "phase": records[-1], "open_phases": records[:-1],
        })
        _ = (self.session / plan_history.STATE_FILENAME).write_text(json.dumps(state))
        for name in ("1", "2"):
            directory = phase_reservation.phase_directory(self.session, name)
            directory.mkdir(parents=True)
            _ = (directory / phase_reservation.FILES_FILE).write_text("scripts/direct/verify.sh\n")
        for name, worktree in worktrees.items():
            code, errors = self.claim(name, worktree)
            self.assertEqual(code, 0, errors)
        return phase_reservation.open_phases(self.session)

    def claim(self, phase: str, root: Path) -> tuple[int, str]:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = phase_reservation.main(["claim", "--session-dir", str(self.session),
                                           "--phase", phase, "--repo", str(root)])
        return code, errors.getvalue()

    def test_missing_unit_row_names_the_owner(self) -> None:
        _ = (self.session / plan_history.STATE_FILENAME).write_text(json.dumps({"owner_staff_id": "missing-owner"}))
        directory = phase_reservation.phase_directory(self.session, "1")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaisesRegex(phase_reservation.StateRefused,
                                                                      "run owner is unavailable"):
            _ = phase_reservation.phase_worktree(directory)
        self.assertIn("run owner missing-owner: unit staff row is unavailable", errors.getvalue())

    def test_claim_records_the_first_commit_and_dirty_status(self) -> None:
        directory = phase_reservation.phase_directory(self.session, "1")
        _ = phase_reservation.enroll(directory, "1", "board_ready")
        _ = (self.first / "untracked.txt").write_text("already here\n")
        head = phase_reservation.commit_id(self.first, "HEAD")
        status = subprocess.run(["git", "-C", str(self.first), "status", "--short"], check=True,
                                capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS).stdout
        self.assertEqual(self.claim("1", self.first), (0, ""))
        self.assertFalse((directory / phase_reservation.WORKTREE_FILE).exists())
        self.assertTrue((directory / phase_reservation.BASELINE_FILE).is_file())
        self.assertEqual((directory / phase_reservation.BASELINE_FILE).read_text(), head + "\n")
        self.assertEqual((directory / phase_reservation.BASELINE_STATUS_FILE).read_text(), status)

    def test_later_claim_keeps_both_first_launch_baselines_after_a_commit(self) -> None:
        directory = phase_reservation.phase_directory(self.session, "1")
        head = phase_reservation.commit_id(self.first, "HEAD")
        _ = (self.first / "untracked.txt").write_text("already here\n")
        self.assertEqual(self.claim("1", self.first), (0, ""))
        _ = subprocess.run(["git", "-C", str(self.first), "add", "untracked.txt"], check=True,
                           capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        _ = subprocess.run(["git", "-C", str(self.first), "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "later"],
                           check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertNotEqual(phase_reservation.commit_id(self.first, "HEAD"), head)
        self.assertEqual(self.claim("1", self.first), (0, ""))
        self.assertTrue((directory / phase_reservation.BASELINE_FILE).is_file())
        self.assertEqual((directory / phase_reservation.BASELINE_FILE).read_text(), head + "\n")
        self.assertEqual((directory / phase_reservation.BASELINE_STATUS_FILE).read_text(), "?? untracked.txt\n")

    def test_claim_without_reservation_state_still_records_both_baselines(self) -> None:
        directory = phase_reservation.phase_directory(self.session, "ad hoc")
        self.assertEqual(self.claim("ad hoc", self.first), (0, ""))
        self.assertFalse((directory / phase_reservation.STATE_FILE).exists())
        self.assertTrue((directory / phase_reservation.BASELINE_FILE).is_file())
        self.assertEqual((directory / phase_reservation.BASELINE_FILE).read_text(),
                         phase_reservation.commit_id(self.first, "HEAD") + "\n")
        self.assertEqual((directory / phase_reservation.BASELINE_STATUS_FILE).read_text(), "")

    def test_claim_supplies_the_baseline_for_record_active_after_first_touch(self) -> None:
        directory = phase_reservation.phase_directory(self.session, "1")
        _ = phase_reservation.enroll(directory, "1", "board_ready")
        self.assertEqual(self.claim("1", self.first), (0, ""))
        _ = (self.first / "changed.txt").write_text("first touch\n")
        answer = phase_reservation.CheckAnswer(phase_reservation.CLEAR, "",
                                              phase_reservation.Acquisition("r1", "c1",
                                                                           phase_reservation.commit_id(self.first, "HEAD")))
        with mock.patch.object(phase_reservation, "check", return_value=answer) as check, \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                code = phase_reservation.main(["record-active", "--session-dir", str(self.session),
                                               "--phase", "1", "--repo", str(self.first)])
            except SystemExit as error:
                self.fail(f"record-active refused after a successful claim: {error}")
        self.assertEqual(code, 0)
        check.assert_called_once_with(self.first, ["changed.txt"], mock.ANY)
        self.assertEqual(phase_reservation.read_state(directory)["kind"], phase_reservation.ACTIVE)

    def test_claim_refuses_a_missing_head_without_publishing_a_baseline(self) -> None:
        _ = subprocess.run(["git", "-C", str(self.first), "checkout", "-q", "--orphan", "unborn"],
                           check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        code, errors = self.claim("1", self.first)
        self.assertEqual(code, 2)
        self.assertIn("phase_reservation:", errors)
        directory = phase_reservation.phase_directory(self.session, "1")
        self.assertFalse((directory / phase_reservation.BASELINE_FILE).exists())
        self.assertFalse((directory / phase_reservation.BASELINE_STATUS_FILE).exists())

    def test_claim_refuses_a_failed_status_without_publishing_a_baseline(self) -> None:
        head = phase_reservation.commit_id(self.first, "HEAD")
        with mock.patch.object(phase_reservation, "repository_root", return_value=self.first), \
                mock.patch.object(phase_reservation, "commit_id", return_value=head), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["git", "status", "--short"], 128, "", "status unavailable")):
            code, errors = self.claim("1", self.first)
        self.assertEqual(code, 2)
        self.assertIn("phase_reservation: git status --short failed", errors)
        directory = phase_reservation.phase_directory(self.session, "1")
        self.assertFalse((directory / phase_reservation.BASELINE_FILE).exists())
        self.assertFalse((directory / phase_reservation.BASELINE_STATUS_FILE).exists())

    def test_interleaved_claims_keep_the_first_launch_commit_and_status(self) -> None:
        directory = phase_reservation.phase_directory(self.session, "1")
        original = phase_reservation.commit_id(self.first, "HEAD")
        _ = (self.first / "first.txt").write_text("first launch\n")
        first_thread = current_thread()
        second_observed = Event()
        first_finished = Event()
        guard = LauncherHangGuard("interleaved phase claims")
        commit = phase_reservation.commit_id
        publish = atomic_file.write_text_atomic
        acquire = worker_lifecycle.acquire_flock
        claims: list[Future[int]] = []

        def second_claim() -> int:
            try:
                return phase_reservation.main(["claim", "--session-dir", str(self.session),
                                               "--phase", "1", "--repo", str(self.first)])
            finally:
                second_observed.set()

        def read_commit(root: Path, name: str) -> str:
            if current_thread() is not first_thread and name == "HEAD":
                # Without serialization the second claim has also observed an absent baseline.
                second_observed.set()
                guard.event(first_finished)
            return commit(root, name)

        def contend(handle: int | worker_lifecycle.LockFileDescriptor, *, mode: int = fcntl.LOCK_EX,
                    deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> None:
            if current_thread() is not first_thread:
                try:
                    acquire(handle, mode=mode | fcntl.LOCK_NB, deadline=deadline)
                    return
                except BlockingIOError:
                    second_observed.set()
            acquire(handle, mode=mode, deadline=deadline)

        with ThreadPoolExecutor(max_workers=1) as worker:
            def interleave(path: Path, content: str) -> None:
                if current_thread() is first_thread and path.name == phase_reservation.BASELINE_STATUS_FILE:
                    claims.append(worker.submit(second_claim))
                    guard.event(second_observed)
                publish(path, content)

            with guard.protect(), mock.patch.object(phase_reservation, "commit_id", read_commit), \
                    mock.patch.object(phase_reservation, "write_text_atomic", interleave), \
                    mock.patch.object(worker_lifecycle, "acquire_flock", contend):
                try:
                    self.assertEqual(self.claim("1", self.first), (0, ""))
                    (self.first / "first.txt").unlink()
                    _ = (self.first / "second.txt").write_text("worker has edited\n")
                    _ = subprocess.run(["git", "-C", str(self.first), "-c", "user.name=Test",
                                        "-c", "user.email=test@example.invalid", "commit", "-q",
                                        "--allow-empty", "-m", "second launch"], check=True,
                                       capture_output=True, timeout=TEST_HANG_GUARD_SECS)
                finally:
                    first_finished.set()
                self.assertEqual(guard.result(claims[0]), 0)
        self.assertEqual((directory / phase_reservation.BASELINE_FILE).read_text(), original + "\n")
        self.assertEqual((directory / phase_reservation.BASELINE_STATUS_FILE).read_text(), "?? first.txt\n")

    def test_claim_uses_the_phase_copy_when_the_launch_folder_lacks_its_baseline(self) -> None:
        phase_copy = self.first.parent / "phase-copy"
        for command in (
            ["git", "clone", "-q", str(self.first), str(phase_copy)],
            ["git", "-C", str(phase_copy), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-q", "--allow-empty", "-m", "phase copy baseline"],
        ):
            self.assertEqual(subprocess.run(command, check=True, capture_output=True,
                                            timeout=TEST_HANG_GUARD_SECS).returncode, 0)
        baseline = phase_reservation.commit_id(phase_copy, "HEAD")
        with self.assertRaisesRegex(phase_reservation.StateRefused, "names no single commit"):
            _ = phase_reservation.commit_id(self.first, baseline)
        for phase, home in (("1", self.first), ("2", phase_copy)):
            with self.subTest(home=home):
                row = next(row for row in staff.snapshot().rows if row.kind == "unit")
                self.assertEqual(staff.set_worktree(row.id, staff.PresentWorktree("natedev", home),
                                                   expected=row.worktree).worktree, staff.PresentWorktree("natedev", home))
                self.assertEqual(self.claim(phase, phase_copy), (0, ""))
                directory = phase_reservation.phase_directory(self.session, phase)
                before = {name: (directory / name).read_bytes() for name in
                          (phase_reservation.BASELINE_FILE, phase_reservation.BASELINE_STATUS_FILE)}
                self.assertEqual(self.claim(phase, self.first), (0, ""))
                self.assertEqual(phase_reservation.phase_worktree(directory), phase_reservation.KnownPhaseWorktree(phase_copy))
                self.assertEqual({name: (directory / name).read_bytes() for name in before}, before)
                self.assertEqual(phase_reservation.commit_id(
                    phase_copy, (directory / phase_reservation.BASELINE_FILE).read_text().strip()), baseline)

    def test_claim_refuses_a_kept_baseline_missing_from_a_replacement_checkout(self) -> None:
        directory = phase_reservation.phase_directory(self.session, "1")
        self.assertEqual(self.claim("1", self.second), (0, ""))
        before = {name: (directory / name).read_bytes() for name in
                  (phase_reservation.BASELINE_FILE, phase_reservation.BASELINE_STATUS_FILE)}
        _ = subprocess.run(["git", "-C", str(self.first), "worktree", "remove", str(self.second)],
                           check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        _ = subprocess.run(["git", "-C", str(self.first), "checkout", "-q", "--orphan", "replacement"],
                           check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        _ = subprocess.run(["git", "-C", str(self.first), "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit", "-q", "--allow-empty",
                            "-m", "replacement"], check=True, capture_output=True,
                           timeout=TEST_HANG_GUARD_SECS)
        # The old root is gone. The new root contains only the unrelated replacement branch.
        replacement = self.first.parent / "replacement"
        _ = subprocess.run(["git", "clone", "-q", "--single-branch", "--no-local", str(self.first),
                            str(replacement)], check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        with self.assertRaises(phase_reservation.StateRefused):
            _ = phase_reservation.commit_id(replacement, before[phase_reservation.BASELINE_FILE].decode().strip())
        code, errors = self.claim("1", replacement)
        self.assertEqual(code, 2)
        self.assertIn("phase_reservation:", errors)
        self.assertIn(str(directory / phase_reservation.BASELINE_FILE), errors)
        self.assertIn(str(replacement), errors)
        self.assertEqual({name: (directory / name).read_bytes() for name in before}, before)

    def test_claim_keeps_a_baseline_present_in_a_replacement_checkout(self) -> None:
        directory = phase_reservation.phase_directory(self.session, "1")
        self.assertEqual(self.claim("1", self.second), (0, ""))
        before = {name: (directory / name).read_bytes() for name in
                  (phase_reservation.BASELINE_FILE, phase_reservation.BASELINE_STATUS_FILE)}
        _ = subprocess.run(["git", "-C", str(self.first), "worktree", "remove", str(self.second)],
                           check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(self.claim("1", self.first), (0, ""))
        self.assertFalse((directory / phase_reservation.WORKTREE_FILE).exists())
        self.assertEqual({name: (directory / name).read_bytes() for name in before}, before)

    def test_claim_retries_after_interruption_between_baseline_writes(self) -> None:
        directory = phase_reservation.phase_directory(self.session, "1")
        publish = atomic_file.write_text_atomic

        def interrupt(path: Path, content: str) -> None:
            if path.name == phase_reservation.BASELINE_FILE:
                raise OSError("interrupted before commit publication")
            publish(path, content)

        _ = (self.first / "before.txt").write_text("interrupted launch\n")
        with mock.patch.object(phase_reservation, "write_text_atomic", interrupt):
            code, errors = self.claim("1", self.first)
        self.assertEqual(code, 2)
        self.assertIn("interrupted", errors)
        self.assertFalse((directory / phase_reservation.BASELINE_FILE).exists())
        (self.first / "before.txt").unlink()
        self.assertEqual(self.claim("1", self.first), (0, ""))
        self.assertEqual((directory / phase_reservation.BASELINE_FILE).read_text(),
                         phase_reservation.commit_id(self.first, "HEAD") + "\n")
        self.assertEqual((directory / phase_reservation.BASELINE_STATUS_FILE).read_text(), "")

    def test_same_file_in_linked_worktrees_belongs_to_each_worktrees_phase(self) -> None:
        for root in (self.first, self.second):
            (root / "src").mkdir()
        phases = self.phases({"1": self.first / "src", "2": self.second / "src"})
        for root, owner in ((self.first, "1"), (self.second, "2")):
            with self.subTest(root=root):
                result = phase_reservation.attribute(phases, ["scripts/direct/verify.sh"], root)
                if isinstance(result, phase_reservation.NoOpenPhaseHere):
                    self.fail(f"claimed phase missing from {root}")
                self.assertEqual(result.refused, [])
                self.assertEqual({phase.phase: scopes for phase, scopes in result.by_phase.items()},
                                 {owner: ["scripts/direct/verify.sh"]})

    def test_only_open_phase_does_not_own_an_edit_in_another_worktree(self) -> None:
        phase = self.phases({"1": self.first, "2": self.second})[0]
        result = phase_reservation.attribute([phase], ["scripts/direct/verify.sh"], self.second)
        self.assertIsInstance(result, phase_reservation.NoOpenPhaseHere)

    def test_attribution_without_an_edit_root_keeps_the_single_phases_ownership(self) -> None:
        phase = self.phases({"1": self.first, "2": self.second})[0]
        result = phase_reservation.attribute([phase], ["unlisted.rs"])
        if isinstance(result, phase_reservation.NoOpenPhaseHere):
            self.fail("rootless attribution lost the claimed phase")
        self.assertEqual(result.refused, [])
        self.assertEqual({owner.phase: scopes for owner, scopes in result.by_phase.items()},
                         {"1": ["unlisted.rs"]})

    def test_same_worktree_overlap_keeps_the_existing_refusal(self) -> None:
        phases = self.phases({"1": self.first, "2": self.first})
        result = phase_reservation.attribute(phases, ["scripts/direct/verify.sh"], self.first)
        if isinstance(result, phase_reservation.NoOpenPhaseHere):
            self.fail("same-worktree phases were filtered out")
        self.assertEqual(result.by_phase, {})
        self.assertEqual(result.refused, ["scripts/direct/verify.sh"])
        self.assertEqual(phase_reservation.refusal_reason(phases, result.refused),
                         "scripts/direct/verify.sh is in no single open phase's files list (open phases: 1, 2); "
                         + "add each path to exactly one phases/<phase>/files.")

    def test_attribution_follows_the_staff_folder_after_a_move(self) -> None:
        _ = self.phases({"1": self.first, "2": self.second})
        directory = phase_reservation.phase_directory(self.session, "1")
        self.assertFalse((directory / phase_reservation.WORKTREE_FILE).exists())
        override = phase_reservation.phase_directory(self.session, "2") / phase_reservation.WORKTREE_FILE
        self.assertEqual(override.read_text(), str(self.second) + "\n")
        row = next(row for row in staff.snapshot().rows if row.kind == "unit")
        destination = self.first.with_name("moved")
        _ = self.first.rename(destination)
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", destination), expected=row.worktree)
        phases = phase_reservation.open_phases(self.session)
        result = phase_reservation.attribute(phases, ["scripts/direct/verify.sh"], destination)
        self.assertIsInstance(result, phase_reservation.Attribution)
        assert isinstance(result, phase_reservation.Attribution)
        self.assertEqual(result.refused, [])
        self.assertEqual([phase.phase for phase in result.by_phase], ["1"])
        self.assertEqual(override.read_text(), str(self.second) + "\n")

    def test_claim_from_a_subfolder_keeps_the_root_after_the_subfolder_is_removed(self) -> None:
        start = self.first / "temporary-start"
        start.mkdir()
        _ = self.phases({"1": start})
        start.rmdir()
        phase = phase_reservation.open_phases(self.session)[0]
        self.assertEqual(phase.worktree, phase_reservation.KnownPhaseWorktree(self.first))
        result = phase_reservation.attribute([phase], ["scripts/direct/verify.sh"], self.first)
        if isinstance(result, phase_reservation.NoOpenPhaseHere):
            self.fail("claim lost its repository root after the start folder was removed")
        self.assertEqual(result.refused, [])
        self.assertEqual({owner.phase: scopes for owner, scopes in result.by_phase.items()},
                         {"1": ["scripts/direct/verify.sh"]})

    def test_held_phase_reader_keeps_the_claimed_worktree(self) -> None:
        _ = self.phases({"1": self.first})
        _ = phase_reservation.enroll(phase_reservation.phase_directory(self.session, "1"), "1", "board_ready")
        phases = phase_reservation.held_phases(self.session)
        self.assertEqual([(phase.phase, phase.worktree) for phase in phases],
                         [("1", phase_reservation.KnownPhaseWorktree(self.first))])


@final
class AcquisitionRecordingTests(unittest.TestCase):
    repo: Path = Path()
    session: Path = Path()
    phases: list[phase_reservation.OpenPhase] = []

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = root / "repo"
        self.repo.mkdir()
        _ = subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True, capture_output=True,
                           timeout=TEST_HANG_GUARD_SECS)
        self.session = root / "session"
        self.phases = []
        for name in ("15", "16"):
            directory = phase_reservation.phase_directory(self.session, name)
            directory.mkdir(parents=True)
            _ = phase_reservation.enroll(directory, name, "board_ready")
            self.phases.append(phase_reservation.OpenPhase(
                name, directory, phase_reservation.KnownPhaseWorktree(self.repo)))
        ledger = self.repo / ".git" / phase_reservation.LEDGER_DIRECTORY
        ledger.mkdir()
        _ = (ledger / phase_reservation.SESSION_MAP_FILE).write_text(json.dumps({"identities": {
            "director": {"reservation_id": "r1", "coordination_run_id": "c1"},
        }}))

    def test_concurrent_phases_record_the_directors_reservation_only_once(self) -> None:
        acquisition = phase_reservation.Acquisition("r1", "c1", "a" * 40)
        answer = phase_reservation.CheckAnswer(phase_reservation.CLEAR, "", acquisition,
                                                phase_reservation.LEFT_UNMAPPED)
        first_inside = Event()
        release_first = Event()
        second_observed = Event()
        second_wrote = Event()
        guard = LauncherHangGuard("concurrent reservation recording")
        record = phase_reservation.record_active
        acquire = worker_lifecycle.acquire_flock

        def record_paused(directory: Path, owned: phase_reservation.Acquisition) -> bool:
            if directory == self.phases[0].directory:
                first_inside.set()
                guard.event(release_first)
            else:
                second_wrote.set()
                second_observed.set()
            return record(directory, owned)

        def contend(handle: int | worker_lifecycle.LockFileDescriptor, *, mode: int = fcntl.LOCK_EX,
                    deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> None:
            if first_inside.is_set():
                try:
                    acquire(handle, mode=mode | fcntl.LOCK_NB, deadline=deadline)
                    return
                except BlockingIOError:
                    second_observed.set()
            acquire(handle, mode=mode, deadline=deadline)

        with guard.protect(), mock.patch.object(phase_reservation, "record_active", record_paused), \
                mock.patch.object(worker_lifecycle, "acquire_flock", contend), \
                ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(phase_reservation.record_own_acquisition, answer, self.repo,
                                   self.phases, "director", self.phases[0])
            try:
                guard.event(first_inside)
                second = workers.submit(phase_reservation.record_own_acquisition, answer, self.repo,
                                        self.phases, "director", self.phases[1])
                guard.event(second_observed)
                self.assertFalse(second_wrote.is_set())
                self.assertEqual(phase_reservation.read_state(self.phases[1].directory), {
                    "kind": phase_reservation.AWAITING_FIRST_TOUCH, "phase": "16"})
            finally:
                release_first.set()
            self.assertEqual(guard.result(first), acquisition)
            self.assertIsNone(guard.result(second))
        self.assertEqual(phase_reservation.read_state(self.phases[0].directory), {
            "kind": phase_reservation.ACTIVE, "active_phase_reservation": {
                "reservation_id": "r1", "coordination_run_id": "c1", "phase": "15",
                "phase_start_head": "a" * 40}})
        self.assertFalse(second_wrote.is_set())
        self.assertEqual(phase_reservation.read_state(self.phases[1].directory), {
            "kind": phase_reservation.AWAITING_FIRST_TOUCH, "phase": "16"})

    def test_a_mapped_acquisition_records_without_a_lock(self) -> None:
        acquisition = phase_reservation.Acquisition("r2", "c1", "a" * 40)
        answer = phase_reservation.CheckAnswer(phase_reservation.CLEAR, "", acquisition)
        with mock.patch.object(worker_lifecycle, "acquire_flock") as acquire:
            result = phase_reservation.record_own_acquisition(
                answer, self.repo, self.phases, "director", self.phases[0])
        acquire.assert_not_called()
        self.assertEqual(result, acquisition)
        self.assertEqual(phase_reservation.read_state(self.phases[0].directory), {
            "kind": phase_reservation.ACTIVE, "active_phase_reservation": {
                "reservation_id": "r2", "coordination_run_id": "c1", "phase": "15",
                "phase_start_head": "a" * 40}})


@final
class PhaseStateTests(unittest.TestCase):
    session: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.session = Path(folder.name)
        record_unit(self, self.session, self.session, "34h")

    def phase_dir(self) -> Path:
        return phase_reservation.phase_directory(self.session, "34h")

    def state(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads((self.phase_dir() / phase_reservation.STATE_FILE).read_text()))

    def run_verb(self, *arguments: str) -> tuple[int, str]:
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            code = phase_reservation.main([*arguments, "--session-dir", str(self.session), "--phase", "34h"])
        return code, errors.getvalue()

    def test_enroll_writes_the_first_state_from_each_board_answer(self) -> None:
        self.assertEqual(self.run_verb("enroll", "--board", "board_ready"), (0, ""))
        self.assertEqual(self.state(), {"kind": "enrolled_awaiting_first_touch", "phase": "34h"})
        self.assertEqual(self.run_verb("enroll", "--board", "unconfigured"), (0, ""))
        self.assertEqual(self.state(), {"kind": "repository_not_enrolled", "phase": "34h"})

    def test_a_held_reservation_is_never_replaced_or_cleared_by_hand(self) -> None:
        phase_reservation._write_state(self.phase_dir(), ACTIVE)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.run_verb("enroll", "--board", "board_ready")[0], 2)
        self.assertEqual(self.run_verb("clear")[0], 2)
        self.assertEqual(self.state(), ACTIVE)

    def test_committed_then_clear_needs_the_released_checkpoint(self) -> None:
        phase_reservation._write_state(self.phase_dir(), ACTIVE)  # pyright: ignore[reportPrivateUsage]
        phase_reservation.committed(self.phase_dir(), "b" * 40)
        pending = cast(dict[str, object], self.state()["checkpoint_release_confirmation_pending"])
        self.assertEqual(pending["checkpoint_commit"], "b" * 40)
        self.assertEqual(pending["reservation_id"], "r1")
        self.assertEqual(self.run_verb("clear", "--released", "c" * 40)[0], 2)
        self.assertEqual(self.run_verb("clear", "--released", "b" * 40), (0, ""))
        self.assertFalse((self.phase_dir() / phase_reservation.STATE_FILE).exists())

    def test_a_pending_record_that_names_no_checkpoint_never_clears_without_one(self) -> None:
        phase_reservation._write_state(self.phase_dir(), {"kind": phase_reservation.PENDING})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.run_verb("clear")[0], 2)
        self.assertEqual(self.state(), {"kind": phase_reservation.PENDING})

    def test_restore_retries_after_release_marker_removal_was_interrupted(self) -> None:
        _ = self.run_verb("enroll", "--board", "board_ready")
        expected = self.state()
        self.assertEqual(self.run_verb("clear", "--released", "b" * 40), (0, ""))
        marker = self.phase_dir() / phase_reservation.RELEASED_FILE
        self.assertTrue(marker.exists())
        unlink = Path.unlink

        def stop_removal(path: Path, missing_ok: bool = False) -> None:
            if path == marker:
                raise InterruptedError("marker removal stopped")
            unlink(path, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", stop_removal), self.assertRaises(InterruptedError):
            _ = phase_reservation.restore(self.phase_dir(), "34h")
        _ = phase_reservation.restore(self.phase_dir(), "34h")
        self.assertEqual(self.state(), expected)
        self.assertFalse(marker.exists())
        self.assertEqual([phase.phase for phase in phase_reservation.open_phases(self.session)], ["34h"])

    def test_a_finished_phase_reopens_with_its_kept_board_answer(self) -> None:
        _ = self.run_verb("enroll", "--board", "board_ready")
        self.assertEqual(self.run_verb("clear"), (0, ""))
        path = self.session / plan_history.STATE_FILENAME
        state = cast(dict[str, object], json.loads(path.read_text()))
        state["phase"] = {"id": "34h", "status": "completed"}
        _ = path.write_text(json.dumps(state))
        self.assertEqual(phase_reservation.open_phases(self.session), [])
        state["phase"] = {"id": "34h", "status": "active"}
        _ = path.write_text(json.dumps(state))
        self.assertEqual(phase_reservation.restore(self.phase_dir(), "34h"), "enrolled_awaiting_first_touch")
        self.assertEqual([phase.phase for phase in phase_reservation.open_phases(self.session)], ["34h"])
        # An open phase is left as it is, and a phase never enrolled has nothing to restore.
        self.assertEqual(phase_reservation.restore(self.phase_dir(), "34h"), "")
        self.assertEqual(phase_reservation.restore(phase_reservation.phase_directory(self.session, "9"), "9"), "")


REPAIR_BERTH = r'''#!__PYTHON__
import json, os, sys
with open(os.environ["REPAIR_BERTH_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
assert sys.argv[1:3] == ["board", "--reservation"]
statuses = json.load(open(os.environ["REPAIR_BOARD"]))
print(json.dumps({"payload": {"data": {"lifecycle": {"status": statuses.get(sys.argv[3], "released")}}}}))
'''
REPAIR_BOARD_POST = r'''#!__PYTHON__
import json, os, sys
with open(os.environ["REPAIR_POST_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
'''
DIRECTOR_SESSION = "director-session"


@final
class PhaseRepairTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    session: Path = Path()
    directory: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        _ = subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True, capture_output=True)
        self.session = self.root / "unit session"
        self.directory = phase_reservation.phase_directory(self.session, "34h")
        self.directory.mkdir(parents=True)
        record_unit(self, self.session, self.repo, "34h")
        _ = (self.directory / phase_reservation.BASELINE_FILE).write_text("a" * 40 + "\n")
        binary = self.root / "bin"
        binary.mkdir()
        for name, source in (("cargo-berth", REPAIR_BERTH), ("bash", REPAIR_BOARD_POST)):
            script = binary / name
            _ = script.write_text(source.replace("__PYTHON__", sys.executable))
            script.chmod(0o755)
        self.environment = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
                            "CLAUDE_CODE_SESSION_ID": DIRECTOR_SESSION,
                            "REPAIR_BERTH_LOG": str(self.root / "berth.log"),
                            "REPAIR_BOARD": str(self.root / "board.json"),
                            "REPAIR_POST_LOG": str(self.root / "posts.log")}
        self.board({"r1": "active"})
        self.recorder_phases("34h")

    def recorder_phases(self, *phases: str) -> None:
        records = [{"id": phase, "status": "active"} for phase in phases]
        state = cast(dict[str, object], json.loads((self.session / plan_history.STATE_FILENAME).read_text()))
        state.update({
            "phase": records[-1] if records else {"id": "34h", "status": "completed"},
            "open_phases": records[:-1]})
        _ = (self.session / plan_history.STATE_FILENAME).write_text(json.dumps(state))

    def test_a_checkpoint_released_phase_cannot_restore_a_live_mapping(self) -> None:
        _ = phase_reservation.enroll(self.directory, "34h", "board_ready")
        _ = phase_reservation.record_active(self.directory, phase_reservation.Acquisition("r1", "c1", "a" * 40))
        phase_reservation.committed(self.directory, "b" * 40)
        self.assertTrue(phase_reservation.clear(self.directory, "b" * 40))
        self.map_sessions({DIRECTOR_SESSION + ".phase-34h": "r1"})
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        result = self.repair()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("checkpoint released its reservation", result.stderr)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)
        self.assertFalse((self.root / "berth.log").exists())
        self.assertFalse((self.root / "posts.log").exists())

    def test_a_finished_phase_cannot_restore_a_live_mapped_reservation(self) -> None:
        self.recorder_phases()
        self.map_sessions({DIRECTOR_SESSION + ".phase-34h": "r1"})
        result = self.repair()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("finished", result.stderr)
        command = shlex.split(result.stderr.splitlines()[-1])
        self.assertEqual(command, ["python3", str(Path(phase_reservation.__file__).with_name("progress_history.py")),
                                   "start-phase", "--session-dir", str(self.session), "--phase-id", "34h",
                                   "--phase-title", "34h"])
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), [phase_reservation.BASELINE_FILE])
        self.assertFalse((self.root / "berth.log").exists())
        self.assertFalse((self.root / "posts.log").exists())

    def board(self, statuses: dict[str, str]) -> None:
        _ = (self.root / "board.json").write_text(json.dumps(statuses))

    def map_sessions(self, sessions: dict[str, str]) -> None:
        ledger = self.repo / ".git" / phase_reservation.LEDGER_DIRECTORY
        ledger.mkdir(exist_ok=True)
        _ = (ledger / phase_reservation.SESSION_MAP_FILE).write_text(json.dumps({"identities": {
            session: {"reservation_id": reservation, "coordination_run_id": "c1"}
            for session, reservation in sessions.items()}}))

    def repair(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, phase_reservation.__file__, "repair", "--session-dir",
                               str(self.session), "--phase", "34h", "--repo", str(self.repo), *arguments],
                              env=self.environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def state(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads((self.directory / phase_reservation.STATE_FILE).read_text()))

    def assert_reopen_refusal(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 2, result.stderr)
        commands = result.stderr.splitlines()[-2:]
        script = str(Path(phase_reservation.__file__).resolve())
        common = ["--session-dir", str(self.session), "--phase", "34h"]
        self.assertEqual([shlex.split(command) for command in commands], [
            ["python3", script, "enroll", *common, "--board", "board_ready"],
            ["python3", script, "claim", *common, "--repo", str(self.repo)]])
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), [phase_reservation.BASELINE_FILE])
        self.assertFalse((self.root / "posts.log").exists())

    def test_a_lost_record_is_restored_from_the_live_phase_mapping_and_logged(self) -> None:
        source = DIRECTOR_SESSION + ".phase-34h"
        self.map_sessions({source: "r1", DIRECTOR_SESSION: "unrelated"})
        result = self.repair()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state(), ACTIVE)
        self.assertEqual((self.root / "berth.log").read_text().splitlines(),
                         [json.dumps(["board", "--reservation", "r1", "--json"])])
        lines = (self.directory / phase_reservation.REPAIRS_FILE).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIsNotNone(datetime.fromisoformat(lines[0].split()[0]).tzinfo)
        self.assertIn("phase=34h reservation_id=r1 run_id=c1 mapping_session=" + source, lines[0])
        posts = (self.root / "posts.log").read_text().splitlines()
        self.assertEqual([cast(list[str], json.loads(post)) for post in posts],
                         [[str(Path(phase_reservation.__file__).with_name("board.sh")), "post", str(self.session),
                           "director", "status", lines[0]]])
        before = (self.directory / phase_reservation.STATE_FILE).read_bytes()
        self.assertEqual(self.repair().returncode, 0)
        self.assertEqual((self.directory / phase_reservation.STATE_FILE).read_bytes(), before)
        self.assertEqual((self.directory / phase_reservation.REPAIRS_FILE).read_text().splitlines(), lines)
        self.assertEqual((self.root / "posts.log").read_text().splitlines(), posts)

    def test_no_mapping_prints_reopen_commands_without_writing(self) -> None:
        self.assert_reopen_refusal(self.repair())

    def test_a_released_mapping_prints_reopen_commands_without_writing(self) -> None:
        self.map_sessions({DIRECTOR_SESSION + ".phase-34h": "r1"})
        self.board({"r1": "released"})
        self.assert_reopen_refusal(self.repair())

    def test_an_existing_live_active_record_is_preserved_byte_for_byte(self) -> None:
        path = self.directory / phase_reservation.STATE_FILE
        original = json.dumps(ACTIVE, indent=2) + "\n"
        _ = path.write_text(original)
        self.map_sessions({DIRECTOR_SESSION + ".phase-34h": "another"})
        result = self.repair()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("r1", result.stdout)
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.directory / phase_reservation.REPAIRS_FILE).exists())
        self.assertFalse((self.root / "posts.log").exists())

    def test_bare_director_mapping_is_accepted_only_without_another_open_phase(self) -> None:
        self.map_sessions({DIRECTOR_SESSION: "r1"})
        sibling = phase_reservation.phase_directory(self.session, "35")
        sibling.mkdir()
        _ = phase_reservation.enroll(sibling, "35", "board_ready")
        self.recorder_phases("34h", "35")
        self.assert_reopen_refusal(self.repair())
        _ = phase_reservation.clear(sibling)
        self.recorder_phases("34h")
        self.assertEqual(self.repair().returncode, 0)
        self.assertEqual(self.state(), ACTIVE)
        self.assertIn("mapping_session=" + DIRECTOR_SESSION,
                      (self.directory / phase_reservation.REPAIRS_FILE).read_text())

    def test_bare_director_mapping_does_not_skip_a_siblings_lost_record(self) -> None:
        self.map_sessions({DIRECTOR_SESSION: "r1"})
        self.recorder_phases("34h", "35")
        phase_reservation.phase_directory(self.session, "35").mkdir()
        self.assert_reopen_refusal(self.repair())

    def test_a_phase_mapping_is_accepted_with_a_sibling_open(self) -> None:
        self.map_sessions({DIRECTOR_SESSION + ".phase-34h": "r1"})
        sibling = phase_reservation.phase_directory(self.session, "35")
        _ = phase_reservation.enroll(sibling, "35", "board_ready")
        self.recorder_phases("34h", "35")
        self.assertEqual(self.repair().returncode, 0)
        self.assertEqual(self.state(), ACTIVE)
        self.assertEqual(phase_reservation.read_state(sibling),
                         {"kind": "enrolled_awaiting_first_touch", "phase": "35"})

    def test_a_missing_baseline_or_unreadable_board_never_creates_an_active_record(self) -> None:
        self.map_sessions({DIRECTOR_SESSION + ".phase-34h": "r1"})
        (self.directory / phase_reservation.BASELINE_FILE).unlink()
        self.assertEqual(self.repair().returncode, 2)
        self.assertEqual(list(self.directory.iterdir()), [])
        _ = (self.directory / phase_reservation.BASELINE_FILE).write_text("a" * 40)
        _ = (self.root / "board.json").write_text("not JSON")
        self.assert_reopen_refusal(self.repair())

    def test_a_released_phase_mapping_does_not_adopt_the_directors_different_reservation(self) -> None:
        self.map_sessions({DIRECTOR_SESSION + ".phase-34h": "released-phase", DIRECTOR_SESSION: "r1"})
        self.assert_reopen_refusal(self.repair())

    def test_pending_checkpoint_release_proof_is_not_overwritten_by_repair(self) -> None:
        _ = phase_reservation.enroll(self.directory, "34h", "board_ready")
        self.assertTrue(phase_reservation.record_active(self.directory, phase_reservation.Acquisition("r1", "c1", "a" * 40)))
        phase_reservation.committed(self.directory, "b" * 40)
        original = (self.directory / phase_reservation.STATE_FILE).read_bytes()
        self.map_sessions({DIRECTOR_SESSION + ".phase-34h": "r1"})
        self.assertEqual(self.repair().returncode, 2)
        self.assertEqual((self.directory / phase_reservation.STATE_FILE).read_bytes(), original)
        self.assertFalse((self.directory / phase_reservation.REPAIRS_FILE).exists())

    def test_repair_does_not_accept_caller_supplied_reservation_or_run_ids(self) -> None:
        self.map_sessions({DIRECTOR_SESSION + ".phase-34h": "r1"})
        for option in ("--reservation-id", "--run-id", "--state"):
            with self.subTest(option=option):
                self.assertEqual(self.repair(option, "forged").returncode, 2)
                self.assertFalse((self.directory / phase_reservation.STATE_FILE).exists())


HELD_BERTH = r'''#!__PYTHON__
import json, os, subprocess, sys
with open(os.environ["HELD_BERTH_LOG"], "a") as log:
    log.write(json.dumps([os.getcwd(), *sys.argv[1:]]) + "\n")
board = json.load(open(os.environ["HELD_BOARD"]))
if sys.argv[1] == "release":
    if board.get("refuse"):
        print(json.dumps({"message": "ledger busy"}))
        sys.exit(6)
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    board[sys.argv[2]] = {"status": "outstanding", "protected_tip": head}
    json.dump(board, open(os.environ["HELD_BOARD"], "w"))
    print(json.dumps({"payload": {"data": {"status": "checkpointed"}}}))
else:
    print(json.dumps({"payload": {"data": {"lifecycle": board.get(sys.argv[3], {"status": "active"})}}}))
'''


@final
class HeldForTicketTests(unittest.TestCase):
    """A style ticket's checkpoint keeps its reservation, and `release-held` releases it at the ticket's merge."""

    root: Path = Path()
    repo: Path = Path()
    session: Path = Path()
    checkpoint: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "unit worktree"
        self.repo.mkdir()
        _ = subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True, capture_output=True)
        self.checkpoint = self.commit("checkpoint")
        self.session = self.root / "unit session"
        phase_reservation._write_state(phase_reservation.phase_directory(self.session, "34h"), ACTIVE)  # pyright: ignore[reportPrivateUsage]
        binary = self.root / "bin"
        binary.mkdir()
        berth = binary / "cargo-berth"
        _ = berth.write_text(HELD_BERTH.replace("__PYTHON__", sys.executable))
        berth.chmod(0o755)
        self.board({})
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
            "HELD_BERTH_LOG": str(self.root / "berth.log"), "HELD_BOARD": str(self.root / "board.json")})))

    def commit(self, message: str) -> str:
        _ = subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "commit", "-q", "--allow-empty", "-m", message], check=True, capture_output=True)
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True).stdout.strip()

    def board(self, lifecycles: dict[str, object]) -> None:
        _ = (self.root / "board.json").write_text(json.dumps(lifecycles))

    def berth_calls(self) -> list[list[str]]:
        log = self.root / "berth.log"
        return [cast(list[str], json.loads(line)) for line in log.read_text().splitlines()] if log.exists() else []

    def run_verb(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = phase_reservation.main([*arguments, "--session-dir", str(self.session), "--phase", "34h"])
        return code, output.getvalue(), errors.getvalue()

    def release(self, ticket: str = "names-1", merged: str = "HEAD") -> tuple[int, str, str]:
        """`release-held` for the ticket's merge of `merged`, the worktree's HEAD unless named."""
        return self.run_verb("release-held", "--ticket", ticket, "--merged", merged)

    def state(self) -> dict[str, object]:
        return phase_reservation.read_state(phase_reservation.phase_directory(self.session, "34h"))

    def hold(self) -> None:
        self.assertEqual(self.run_verb("committed", "--commit", self.checkpoint, "--ticket", "names-1", "--repo",
                                       str(self.repo))[0], 0)

    def test_a_style_checkpoint_keeps_the_reservation_until_its_ticket_merge_releases_it(self) -> None:
        self.hold()
        self.assertEqual(self.state(), {"kind": phase_reservation.PENDING, phase_reservation.PENDING_FIELD: {
            **cast(dict[str, object], ACTIVE["active_phase_reservation"]), "checkpoint_commit": self.checkpoint,
            "held_for_ticket": "names-1", "worktree": str(self.repo)}})
        self.assertEqual(self.berth_calls(), [])
        self.assertEqual(self.release(), (0, "phase 34h: released reservation r1\n", ""))
        self.assertIn([str(self.repo), "release", "r1", "--json"], self.berth_calls())
        self.assertEqual(self.state(), {})
        phase_dir = phase_reservation.phase_directory(self.session, "34h")
        self.assertEqual((phase_dir / phase_reservation.RELEASED_FILE).read_text(), self.checkpoint + "\n")
        calls = self.berth_calls()
        # A repeated merge finds no held reservation and calls berth no more.
        self.assertEqual(self.release(),
                         (0, "phase 34h: no reservation held for ticket names-1\n", ""))
        self.assertEqual(self.berth_calls(), calls)

    def test_another_tickets_merge_and_a_plain_checkpoint_release_nothing(self) -> None:
        self.hold()
        self.assertEqual(self.release("names-2")[:2],
                         (0, "phase 34h: no reservation held for ticket names-2\n"))
        self.assertEqual(self.state()["kind"], phase_reservation.PENDING)
        phase_reservation._write_state(phase_reservation.phase_directory(self.session, "34h"), ACTIVE)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.run_verb("committed", "--commit", self.checkpoint, "--repo", str(self.repo)),
                         (0, f"phase 34h: {phase_reservation.PENDING}\n", ""))
        # Every other unit's checkpoint writes the pending state as before, and ticket merge never releases it.
        self.assertEqual(self.state(), {"kind": phase_reservation.PENDING, phase_reservation.PENDING_FIELD: {
            **cast(dict[str, object], ACTIVE["active_phase_reservation"]), "checkpoint_commit": self.checkpoint}})
        self.assertEqual(self.release()[0], 0)
        self.assertEqual(self.state()["kind"], phase_reservation.PENDING)
        self.assertEqual(self.berth_calls(), [])

    def test_a_worktree_off_its_checkpoint_keeps_the_reservation_held(self) -> None:
        self.hold()
        moved = self.commit("later")
        code, _, errors = self.release()
        self.assertEqual(code, 2)
        self.assertIn(f"is at {moved[:7]}, not checkpoint {self.checkpoint[:7]}; reservation r1 stays held", errors)
        self.assertNotIn("release", [call[1] for call in self.berth_calls()])
        self.assertEqual(self.state()["kind"], phase_reservation.PENDING)

    def test_a_release_berth_does_not_confirm_keeps_the_reservation_held(self) -> None:
        self.hold()
        self.board({"refuse": True})
        code, _, errors = self.release()
        self.assertEqual(code, 2)
        self.assertIn(f"berth did not release reservation r1 at checkpoint {self.checkpoint[:7]}: ledger busy", errors)
        self.assertEqual(self.state()["kind"], phase_reservation.PENDING)
        # A checkpoint berth holds at another tip is another release: this one is not made.
        self.board({"r1": {"status": "outstanding", "protected_tip": "c" * 40}})
        code, _, errors = self.release()
        self.assertEqual(code, 2)
        self.assertIn(f"berth shows reservation r1 outstanding, not active or released at checkpoint {self.checkpoint[:7]}", errors)
        self.assertEqual(self.state()["kind"], phase_reservation.PENDING)
        self.assertEqual([call[1] for call in self.berth_calls()].count("release"), 1)

    def test_a_release_an_earlier_merge_made_is_confirmed_without_another(self) -> None:
        for status in ("outstanding", "released_after_checkpoint"):
            with self.subTest(status=status):
                phase_reservation._write_state(phase_reservation.phase_directory(self.session, "34h"), ACTIVE)  # pyright: ignore[reportPrivateUsage]
                self.hold()
                self.board({"r1": {"status": status, "protected_tip": self.checkpoint}})
                self.assertEqual(self.release()[0], 0)
                self.assertEqual(self.state(), {})
        self.assertEqual({call[1] for call in self.berth_calls()}, {"board"})

    def commit_tree(self, message: str, *parents: str) -> str:
        """A commit off the worktree's branch on these parents; with none it shares no history with the checkpoint."""
        tree = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD^{tree}"], check=True, capture_output=True,
                              text=True).stdout.strip()
        options = [option for parent in parents for option in ("-p", parent)]
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               "commit-tree", tree, *options, "-m", message], check=True, capture_output=True,
                              text=True).stdout.strip()

    def assert_held_at(self, tip: str, merged: str) -> None:
        """berth's release at `tip` does not count for the merge of `merged`: the reservation stays held."""
        self.board({"r1": {"status": "released_after_checkpoint", "protected_tip": tip}})
        code, _, errors = self.release(merged=merged)
        self.assertEqual(code, 2)
        self.assertIn(f"berth shows reservation r1 released_after_checkpoint, not active or released at checkpoint "
                      + f"{self.checkpoint[:7]} or at a later commit that {merged[:7]} contains; it stays held", errors)
        self.assertEqual(self.state()["kind"], phase_reservation.PENDING)
        self.assertEqual({call[1] for call in self.berth_calls()}, {"board"})

    def test_a_release_at_a_later_merge_the_merged_commit_contains_is_confirmed(self) -> None:
        self.hold()
        # The unit merges style/main after its checkpoint and berth releases there (cargo-liner ticket 90).
        later = self.commit("merge style/main")
        merged = self.commit_tree("showrunner merge", later)
        self.board({"r1": {"status": "released_after_checkpoint", "protected_tip": later}})
        self.assertEqual(self.release(merged=merged[:8]), (0, "phase 34h: released reservation r1\n", ""))
        self.assertEqual(self.state(), {})
        self.assertEqual({call[1] for call in self.berth_calls()}, {"board"})

    def test_a_release_at_a_tip_after_no_checkpoint_keeps_the_reservation_held(self) -> None:
        self.hold()
        unrelated = self.commit_tree("another branch")
        self.assert_held_at(unrelated, self.commit_tree("showrunner merge", self.checkpoint, unrelated))

    def test_a_release_at_a_later_tip_the_merged_commit_lacks_keeps_the_reservation_held(self) -> None:
        self.hold()
        later = self.commit("merge style/main")
        self.assert_held_at(later, self.commit_tree("showrunner merge", self.checkpoint))

    def test_a_release_at_the_checkpoint_counts_whatever_the_merged_commit(self) -> None:
        self.hold()
        self.board({"r1": {"status": "released_after_checkpoint", "protected_tip": self.checkpoint}})
        self.assertEqual(self.release(merged=self.commit_tree("another branch"))[0], 0)
        self.assertEqual(self.state(), {})

    def test_a_merged_name_that_is_no_commit_keeps_the_reservation_held(self) -> None:
        self.hold()
        code, _, errors = self.release(merged="no-such-commit")
        self.assertEqual(code, 2)
        self.assertIn(f"merged commit 'no-such-commit' names no single commit in {self.repo}; reservation r1 stays held", errors)
        self.assertEqual(self.berth_calls(), [])
        self.assertEqual(self.state()["kind"], phase_reservation.PENDING)

    def record_checkpoint(self, commit: str) -> None:
        """Rewrite the pending record's checkpoint, as cargo-liner ticket 59's record held it in short form."""
        phase_dir = phase_reservation.phase_directory(self.session, "34h")
        state = phase_reservation.read_state(phase_dir)
        pending = cast(dict[str, object], state[phase_reservation.PENDING_FIELD])
        phase_reservation._write_state(phase_dir, {**state, phase_reservation.PENDING_FIELD: {  # pyright: ignore[reportPrivateUsage]
            **pending, "checkpoint_commit": commit}})

    def test_committed_records_the_full_id_of_any_commit_name(self) -> None:
        for ticket in ("names-1", ""):
            with self.subTest(ticket=ticket):
                phase_reservation._write_state(phase_reservation.phase_directory(self.session, "34h"), ACTIVE)  # pyright: ignore[reportPrivateUsage]
                held = ("--ticket", ticket) if ticket else ()
                self.assertEqual(self.run_verb("committed", "--commit", self.checkpoint[:7], *held, "--repo", str(self.repo))[0], 0)
                pending = cast(dict[str, object], self.state()[phase_reservation.PENDING_FIELD])
                self.assertEqual(pending["checkpoint_commit"], self.checkpoint)

    def test_committed_refuses_a_name_that_is_no_commit(self) -> None:
        tree = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD^{tree}"], check=True, capture_output=True,
                              text=True).stdout.strip()
        for name in ("no-such-commit", tree):
            with self.subTest(name=name):
                code, _, errors = self.run_verb("committed", "--commit", name, "--ticket", "names-1", "--repo", str(self.repo))
                self.assertEqual(code, 2)
                self.assertIn(f"{name!r} names no single commit in {self.repo}", errors)
                self.assertEqual(self.state(), ACTIVE)

    def test_clear_resolves_the_released_name_in_its_repo(self) -> None:
        self.hold()
        other = self.root / "other repo"
        other.mkdir()
        _ = subprocess.run(["git", "-C", str(other), "init", "-q"], check=True, capture_output=True)
        # A prefix of the checkpoint that the named repo does not resolve, as an ambiguous one, is refused.
        code, _, errors = self.run_verb("clear", "--released", self.checkpoint[:7], "--repo", str(other))
        self.assertEqual(code, 2)
        self.assertIn(f"{self.checkpoint[:7]!r} names no single commit in {other}", errors)
        self.assertEqual(self.state()["kind"], phase_reservation.PENDING)
        # A name that is no prefix of the checkpoint, and resolves to it, is taken; the marker keeps the full id.
        self.assertEqual(self.run_verb("clear", "--released", "HEAD", "--repo", str(self.repo)), (0, "phase 34h: cleared\n", ""))
        self.assertEqual(self.state(), {})
        phase_dir = phase_reservation.phase_directory(self.session, "34h")
        self.assertEqual((phase_dir / phase_reservation.RELEASED_FILE).read_text(), self.checkpoint + "\n")

    def test_a_record_stored_short_clears_with_that_string_alone(self) -> None:
        self.hold()
        self.record_checkpoint(self.checkpoint[:8])
        self.assertEqual(self.run_verb("clear", "--released", self.checkpoint[:8], "--repo", str(self.root / "no repo"))[0], 0)
        self.assertEqual(self.state(), {})

    def test_a_short_record_released_by_berth_at_its_checkpoint_clears(self) -> None:
        self.hold()
        self.record_checkpoint(self.checkpoint[:8])
        self.board({"r1": {"status": "released_after_checkpoint", "protected_tip": self.checkpoint,
                           "disposition": "integrated"}})
        self.assertEqual(phase_reservation.release_held(phase_reservation.phase_directory(self.session, "34h"), "names-1", "HEAD"), "r1")
        self.assertEqual(self.state(), {})
        self.assertEqual({call[1] for call in self.berth_calls()}, {"board"})

    def test_a_short_record_with_an_active_reservation_releases_at_its_checkpoint(self) -> None:
        self.hold()
        self.record_checkpoint(self.checkpoint[:8])
        self.assertEqual(self.release(), (0, "phase 34h: released reservation r1\n", ""))
        self.assertIn([str(self.repo), "release", "r1", "--json"], self.berth_calls())
        self.assertEqual(self.state(), {})

    def test_a_recorded_checkpoint_that_names_no_commit_keeps_the_reservation_held(self) -> None:
        self.hold()
        self.record_checkpoint("c" * 40)
        code, _, errors = self.release()
        self.assertEqual(code, 2)
        self.assertIn(f"checkpoint {'c' * 40!r} names no single commit in {self.repo}; reservation r1 stays held", errors)
        self.assertEqual(self.berth_calls(), [])
        self.assertEqual(self.state()["kind"], phase_reservation.PENDING)


if __name__ == "__main__":
    _ = unittest.main()
