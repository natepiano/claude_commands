"""Use the same Darwin start identity for claim adoption and PID reuse."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import sys

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.mac_test import claim_state, mac_test
from scripts.lib import process_start as identity


class ClaimProcessStartTests(unittest.TestCase):
    def test_darwin_adoption_requires_both_live_start_matches(self) -> None:
        old_start = "Tue Nov  3 23:00:00 2026"
        new_start = "Wed Nov  4 00:00:00 2026"
        matching = claim_state.ClaimIdentity("claim-a", "episode-a", "attempt-a")
        cases = (
            (True, True, matching, NoOwningSession(), False, 0),
            (False, True, matching, NoOwningSession(), False, 12),
            (True, False, matching, NoOwningSession(), False, 12),
            (True, True, claim_state.ClaimIdentity("claim-b", "episode-a", "attempt-a"), NoOwningSession(), False, 12),
            (True, True, claim_state.ClaimIdentity("claim-a", "episode-b", "attempt-a"), NoOwningSession(), False, 12),
            (True, True, claim_state.ClaimIdentity("claim-a", "episode-a", "attempt-b"), NoOwningSession(), False, 12),
            (True, True, matching, OwningSession("another-session"), False, 12),
            (True, True, claim_state.ClaimIdentity("claim-b", "episode-a", "attempt-b"), OwningSession("another-session"), False, 12),
            (True, True, matching, OwningSession("cleanup-session"), True, 13),
            (True, True, claim_state.ClaimIdentity("claim-b", "episode-a", "attempt-a"), OwningSession("cleanup-session"), True, 12),
            (True, False, matching, OwningSession("cleanup-session"), True, 12),
        )
        for owner_matches, runner_live, requested, session, cleanup, expected in cases:
            with self.subTest(owner_matches=owner_matches, runner_live=runner_live,
                              identity=requested, session=session, cleanup=cleanup), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = mac_test.StatePaths(root)
                mac_test.write_json(paths.run, {"claim_id": "claim-a", "episode": "episode-a", "attempt": "attempt-a",
                    "submitting_session": "original-session" if cleanup else "", "pid": 41, "proc_start": old_start, "what": "waiting",
                    "worktree": str(root), "since": "2026-11-04T00:00:00+00:00"})
                if cleanup:
                    retained = mac_test.read_run(paths.run)
                    assert not isinstance(retained, mac_test.NoRun)
                    mac_test.write_json(paths.run, {**retained, "remote_host": "mac"})
                args = mac_test.CliArguments()
                args.pid, args.from_pid, args.worktree, args.remote = 42, 41, str(root), "mac"
                args.claim_id, args.episode, args.attempt = requested.claim_id, requested.episode, requested.attempt

                def ps(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    pid = command[2]
                    if pid == "42" and not runner_live:
                        return subprocess.CompletedProcess(command, 1, "", "")
                    start = old_start if pid == "41" and owner_matches else new_start
                    return subprocess.CompletedProcess(command, 0, start + " S\n", "")

                with mock.patch.object(sys, "platform", "darwin"), \
                        mock.patch.object(subprocess, "run", side_effect=ps), \
                        mock.patch.object(mac_test, "target_claim_scope", return_value=mac_test.WHOLE_MAC), \
                        mock.patch.object(mac_test, "bind_instance_generation"), redirect_stdout(io.StringIO()):
                    if cleanup:
                        held = mac_test.read_claims(paths)[0]
                        assert "remote_host" in held.record
                        takeover = mac_test.CliArguments()
                        takeover.pid = 41
                        transition = mac_test.take_over_kept_claim(
                            takeover, held, held.record, session=OwningSession("cleanup-session"))
                        self.assertIsInstance(transition, mac_test.ClaimFinished)
                        assert isinstance(transition, mac_test.ClaimFinished)
                        self.assertEqual(transition.status, 13)
                        before = paths.run.read_bytes()
                        unrelated = mac_test.adopt_wait_claim(
                            args, paths, mac_test.WHOLE_MAC, session=OwningSession("unrelated-session"))
                        self.assertIsInstance(unrelated, mac_test.ClaimFinished)
                        assert isinstance(unrelated, mac_test.ClaimFinished)
                        self.assertEqual(unrelated.status, 12)
                        self.assertEqual(paths.run.read_bytes(), before)
                    result = mac_test.adopt_wait_claim(args, paths, mac_test.WHOLE_MAC, session=session)
                self.assertIsInstance(result, mac_test.ClaimFinished)
                assert isinstance(result, mac_test.ClaimFinished)
                self.assertEqual(result.status, expected)
                saved = mac_test.read_run(paths.run)
                self.assertNotIsInstance(saved, mac_test.NoRun)
                assert not isinstance(saved, mac_test.NoRun)
                self.assertEqual(saved["pid"], 42 if expected in (0, 13) else 41)
                self.assertEqual(saved["proc_start"], new_start if expected in (0, 13) else old_start)
                self.assertEqual((saved["claim_id"], saved["episode"], saved["attempt"]),
                                 ("claim-a", "episode-a", "attempt-a"))

    def test_pid_reuse_and_unreadable_start_never_allow_a_signal(self) -> None:
        claim: mac_test.ProcessBoundClaim = {"claim_id": "claim-a", "episode": "episode-a", "attempt": "attempt-a",
            "submitting_session": "", "pid": os.getpid(), "proc_start": "old", "what": "copy",
            "worktree": "/tmp/test", "since": "2026-11-04T00:00:00+00:00", "copy": True}
        for result in (identity.Started("different"), identity.Zombie("old"), identity.Gone(), identity.Unreadable("denied")):
            with self.subTest(result=type(result)), mock.patch.object(identity, "process_start", return_value=result), \
                    mock.patch.object(os, "kill") as send:
                self.assertFalse(mac_test.run_is_live(claim))
                self.assertFalse(mac_test.copy_gives_way(claim))
                mac_test.stop_copy(claim)
                send.assert_not_called()
