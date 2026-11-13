"""Use the same Darwin start identity for claim adoption and PID reuse."""
from __future__ import annotations

from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import Literal
from unittest import mock

import sys

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.mac_test import claim_state, mac_test
from scripts.lib import process_start as identity
from scripts.build_queue import placement, admission_protocol as wire
from scripts.build_queue.test_placement import (
    PlacementFixture, placement_request, publish_candidate, assignment_for, coordinator_holder,
)
from scripts.lint.memory_test_support import write_config_fixture


class ClaimProcessStartTests(unittest.TestCase):
    def test_assigned_claims_keep_provider_overlap_rules_and_never_substitute_a_linux_slot(self) -> None:
        cases: tuple[tuple[Literal["mac", "ssh"], claim_state.ClaimScope, claim_state.ClaimScope, list[int]], ...] = (
            ("mac", mac_test.RepositoryScope("hana"), mac_test.RepositoryScope("hana"), [0, 11]),
            ("mac", mac_test.RepositoryScope("hana"), mac_test.RepositoryScope("other"), [0, 0]),
            ("mac", mac_test.WHOLE_MAC, mac_test.RepositoryScope("hana"), [0, 11]),
            ("ssh", mac_test.RepositoryScope("hana", "repo-key", 1), mac_test.RepositoryScope("hana", "repo-key", 1), [0, 11]),
        )
        for provider, first_scope, second_scope, expected in cases:
            with self.subTest(provider=provider, first=first_scope, second=second_scope), PlacementFixture() as fixture:
                target = "mac" if provider == "mac" else "box"
                _ = (fixture.root / "build_targets.conf").write_text(
                    "targets=mac,box\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
                    + "kind.box=ssh\nplatform.box=linux\nsettings.box=box.conf\n")
                _ = write_config_fixture(fixture.root / "box.conf", "offload=on\nslots=2\n", template="mac_test")
                paths = mac_test.StatePaths(fixture.root / "provider", target,
                    platform="macos" if provider == "mac" else "linux")
                assignments: list[placement.PlacementAssignment] = []
                requests: list[wire.PlacementRequest] = []
                for scope in (first_scope, second_scope):
                    request = placement_request(fixture)
                    destination = placement.PlacementDestination(provider, target, "fixture-host", scope,
                        scope.slot if provider == "ssh" and isinstance(scope, mac_test.RepositoryScope) else 0)
                    request = replace(request, constraints=replace(request.constraints,
                        candidates=(placement.PlacementCandidate(destination),)))
                    _ = publish_candidate(fixture, request)
                    assignments.append(assignment_for(fixture, request))
                    requests.append(request)

                def acquire(assignment: placement.PlacementAssignment) -> int:
                    args = mac_test.CliArguments()
                    args.pid, args.target, args.worktree = assignment.owner.pid, target, str(fixture.root)
                    args.what = "nextest hana"
                    return mac_test.command_claim(args, paths, watch_claim=False,
                        session=assignment.session, assignment=assignment)

                with redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=2) as workers:
                    statuses = list(workers.map(acquire, assignments))
                self.assertEqual(sorted(statuses), expected)
                held = mac_test.read_claims(paths)
                self.assertEqual(len(held), expected.count(0))
                if provider == "ssh":
                    self.assertEqual([claim.scope.slot for claim in held if isinstance(claim.scope, mac_test.RepositoryScope)], [1])
                authorizations = 0
                for request, assigned, status in zip(requests, assignments, statuses, strict=True):
                    if status != 0:
                        continue
                    claim, = [claim for claim in held if claim.record["pid"] == assigned.owner.pid]
                    exact = claim_state.claim_identity(claim.record)
                    args = mac_test.CliArguments()
                    args.pid, args.from_pid, args.target = assigned.owner.pid, assigned.owner.pid, target
                    args.worktree, args.remote = str(fixture.root), assigned.destination.host
                    args.claim_id, args.episode, args.attempt = exact.claim_id, exact.episode, exact.attempt
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(mac_test.command_claim(args, paths, watch_claim=False,
                            session=assigned.session, assignment=assigned), 0)
                    reference = placement.ProviderClaimReference(provider, target, assigned.destination.scope,
                        exact.claim_id, exact.episode, exact.attempt)
                    holder = coordinator_holder(assigned)
                    adopted = placement.AdoptedProviderClaim(reference, holder, assigned.session,
                        assigned.destination.host, "host-generation-a")
                    _ = publish_candidate(fixture, request, claims=(adopted,))
                    reply, = fixture.connect(wire.ConsumePermission("winner-" + assigned.episode,
                        assigned, reference, holder)).replies()
                    self.assertIsInstance(reply, wire.FirstAuthorization)
                    authorizations += isinstance(reply, wire.FirstAuthorization)
                self.assertEqual(authorizations, expected.count(0))

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
                    if cleanup and expected == 13:
                        assigned = placement.PlacementAssignment("new-episode", "new-attempt",
                            placement.PlacementDestination("mac", "mac", "mac", mac_test.WHOLE_MAC),
                            placement.SourceKeyUnavailable("the retained cleanup has no source key"),
                            placement.SubmittingOwner("natedev", 42, new_start), session, 0)
                        result_status = mac_test.command_claim(args, paths, watch_claim=False,
                            session=session, assignment=assigned)
                        result = mac_test.ClaimFinished(result_status)
                    else:
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
