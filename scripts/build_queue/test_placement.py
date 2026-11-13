"""Placement preserves attempt identity and grants one execution authorization."""
from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
from typing import TextIO, cast, override

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_queue import admission_protocol as wire, admission_records as records, admission_service as service, placement
from scripts.build_queue.admission_test_support import AdmissionFixture, ObservedAdmissionServer, only_admitted, GIB
from scripts.mac_test.claim_state import RepositoryScope, WholeMacScope
from scripts.mac_test import claim_state, mac_test, offload
from scripts.lint.memory_test_support import write_config_fixture, write_pressure_reading
from scripts.lint import memory_admit as gate
from scripts.message.unit_labels import UnitLabels
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.build_queue.queue_fixture import write_process


SUBMITTING_SESSION = OwningSession("submitting-session")
ALLOWED_HOST = placement.AllowedHost()


class PlacementFixture(AdmissionFixture):
    """Supply private provider evidence across each service restart."""

    read_host_evidence: Callable[[placement.HostEvidenceKey], placement.HostEvidence]

    def __init__(self) -> None:
        self.provider_evidence: dict[placement.HostEvidenceKey, placement.HostEvidence] = {}
        super().__init__()

    @override
    def read_provider(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        if key.destination.provider == "local" and key not in self.provider_evidence:
            return placement.FreshHostEvidence(key, self.clock.now(), "", placement.AllowedHost())
        return self.provider_evidence.get(key, placement.HostEvidenceUnavailable(key, self.clock.now(), "not read"))

    @override
    def new_server(self, *, start: bool = True) -> ObservedAdmissionServer:
        server = super().new_server(start=False)
        self.read_host_evidence = server.read_provider_evidence
        server.host_probe = self.read_provider
        if start:
            server.start()
        return server


def placement_request(fixture: AdmissionFixture, *, session: placement.SubmittingSession = SUBMITTING_SESSION,
                      since: float | None = None) -> wire.PlacementRequest:
    local = fixture.request(since=since)
    local = replace(local, run=wire.RunIdentity(local.owner.pid, local.owner.start_time))
    destination = placement.PlacementDestination("mac", "mac", "fixture-mac", RepositoryScope("hana"))
    constraints = placement.PlacementConstraints(placement.SourceKeyKnown("source-a"), ("cargo", "nextest", "run"), "hana", "hana", session,
                                                 (placement.PlacementCandidate(destination),))
    owner = placement.SubmittingOwner(fixture.server.host, local.owner.pid, local.owner.start_time)
    return wire.PlacementRequest(local.episode, owner, local.since, local.profile, constraints,
                                 identity=local.identity, run=local.run,
                                 local=wire.LocalAdmissionContext(constraints.command, local.cwd, local.sidecar,
                                     local.call, local.test_width, local.fresh_test),
                                 code_version=local.code_version)


def coordinator_holder(assignment: placement.PlacementAssignment) -> placement.ClaimHolderIdentity:
    return placement.ClaimHolderIdentity(assignment.owner.host, assignment.owner.pid, assignment.owner.start_time)


def consume_permission(request_id: str, assignment: placement.PlacementAssignment,
                       claim: placement.ProviderClaimReference) -> wire.ConsumePermission:
    return wire.ConsumePermission(request_id, assignment, claim, holder=coordinator_holder(assignment))


def local_candidate(fixture: AdmissionFixture) -> placement.PlacementCandidate:
    return placement.PlacementCandidate(placement.PlacementDestination(
        "local", "natedev", fixture.server.host, WholeMacScope()))


def publish_candidate(fixture: PlacementFixture, request: wire.PlacementRequest,
                      eligibility: placement.HostEligibility = ALLOWED_HOST,
                      claims: tuple[placement.AdoptedProviderClaim, ...] = ()) -> placement.FreshHostEvidence:
    destination = request.constraints.candidates[0].destination
    key = fixture.server.host_evidence_key(request, destination)
    evidence = placement.FreshHostEvidence(key, fixture.clock.now(), "host-generation-a", eligibility, claims)
    fixture.provider_evidence[key] = evidence
    fixture.server.publish_host_evidence(evidence)
    return evidence


def assignment_for(fixture: AdmissionFixture, request: wire.PlacementRequest) -> placement.PlacementAssignment:
    reply, = fixture.server.handle_request(request)
    if not isinstance(reply, wire.Assignment):
        raise AssertionError(f"placement did not assign the eligible destination: {reply!r}")
    return reply.assignment


def adopt_assignment(fixture: PlacementFixture, request: wire.PlacementRequest,
                     assignment: placement.PlacementAssignment) -> placement.ProviderClaimReference:
    destination = assignment.destination
    reference = placement.ProviderClaimReference(destination.provider, destination.target, destination.scope,
                                                  f"claim-{assignment.attempt}", assignment.episode, assignment.attempt)
    adopted = placement.AdoptedProviderClaim(reference, coordinator_holder(assignment), assignment.session,
                                             destination.host, "host-generation-a")
    _ = publish_candidate(fixture, request, claims=(adopted,))
    return reference


class PlacementTests(unittest.TestCase):
    def test_blocked_provider_claim_and_cleanup_keep_local_socket_requests_responsive(self) -> None:
        for operation in ("claim", "cleanup"):
            with self.subTest(operation=operation), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                reference = adopt_assignment(fixture, request, assignment)
                paths = mac_test.StatePaths(fixture.root / "blocked-provider")
                paths.directory.mkdir()
                record: mac_test.RemoteJobClaim = {"claim_id": reference.claim_id, "episode": reference.episode,
                    "attempt": reference.attempt, "submitting_session": "submitting-session",
                    "pid": request.owner.pid, "proc_start": request.owner.start_time, "remote_host": assignment.destination.host,
                    "what": "blocked provider", "worktree": str(fixture.root), "since": "2026-11-10T12:00:00+00:00"}
                mac_test.write_json(paths.run, record)
                before = paths.run.read_bytes()
                granted = only_admitted(fixture.connect(fixture.request()).replies())
                locked, started, finish = threading.Event(), threading.Event(), threading.Event()

                def hold_provider_lock() -> None:
                    with mac_test.locked(paths.lock):
                        locked.set()
                        if not finish.wait(TEST_HANG_GUARD_SECS):
                            raise AssertionError("The fixture did not release the provider lock")

                def adapter() -> int:
                    args = mac_test.CliArguments()
                    args.pid, args.worktree, args.remote = request.owner.pid, str(fixture.root), assignment.destination.host
                    args.claim_id, args.episode, args.attempt = reference.claim_id, reference.episode, reference.attempt
                    started.set()
                    with redirect_stdout(io.StringIO()):
                        if operation == "cleanup":
                            return mac_test.command_release(args, paths)
                        return mac_test.command_claim(args, paths, watch_claim=False,
                            session=assignment.session, assignment=assignment)

                with ThreadPoolExecutor(max_workers=2) as workers:
                    lock_owner = workers.submit(hold_provider_lock)
                    try:
                        self.assertTrue(locked.wait(1))
                        pending = workers.submit(adapter)
                        self.assertTrue(started.wait(1))
                        self.assertFalse(pending.done())
                        fixture.allow_next_start()
                        for message in (wire.ViewRequest(), wire.ReleaseRequest("release-responsive", granted.reservation, 0),
                                        fixture.request()):
                            began = time.monotonic()
                            replies = fixture.connect(message).replies()
                            self.assertLess(time.monotonic() - began, 1, type(message).__name__)
                            self.assertTrue(replies)
                            self.assertIsInstance(replies[-1], wire.ViewReply if isinstance(message, wire.ViewRequest)
                                else wire.Ok if isinstance(message, wire.ReleaseRequest) else wire.Admitted)
                        self.assertFalse(pending.done())
                        self.assertEqual(paths.run.read_bytes(), before)
                    finally:
                        finish.set()
                    _ = lock_owner.result(timeout=TEST_HANG_GUARD_SECS)
                    _ = pending.result(timeout=TEST_HANG_GUARD_SECS)

    def test_speculative_placement_keeps_the_newest_live_candidate_and_its_local_parent_charge(self) -> None:
        cases = (
            ((("v1", 100.0, True), ("v2", 200.0, True), ("v3", 300.0, False)), "v2"),
            ((("v-a", 100.0, True), ("v-b", 100.0, True)), "v-b"),
            ((), ""),
            ((("v1", 100.0, False),), ""),
        )
        for candidates, expected in cases:
            with self.subTest(candidates=candidates), PlacementFixture() as fixture:
                parent = fixture.request(run=fixture.run_owner())
                grant = only_admitted(fixture.connect(parent).replies())
                submitted: list[wire.PlacementRequest] = []
                for episode, started, live in candidates:
                    request = replace(placement_request(fixture), episode=episode,
                        purpose=placement.SpeculativeCopy(parent.episode, started))
                    _ = publish_candidate(fixture, request, placement.TemporaryExclusion("busy"))
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    submitted.append(request)
                    if not live:
                        fixture.end_owner(wire.Owner(request.owner.pid, request.owner.start_time))
                for request in submitted:
                    _ = publish_candidate(fixture, request)
                _ = fixture.run_pass()
                selected = [request.episode for request in submitted
                            if request.episode in fixture.server.current_assignments]
                self.assertEqual(selected, [expected] if expected else [])
                for _ in range(2):
                    _ = fixture.run_pass()
                    self.assertEqual([request.episode for request in submitted
                                      if request.episode in fixture.server.current_assignments], selected)
                self.assertEqual(set(fixture.server.reservations), {parent.episode})
                self.assertEqual(fixture.server.reservations[parent.episode].reservation_id, grant.reservation)

    def test_fixed_and_movable_waiters_share_order_and_priority_changes(self) -> None:
        for prioritize_movable in (False, True):
            with self.subTest(prioritize_movable=prioritize_movable), PlacementFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                fixed = fixture.request(since=fixture.clock.now() - 60, run=fixture.run_owner())
                movable = placement_request(fixture, since=fixture.clock.now() - 10)
                movable = replace(movable, constraints=replace(movable.constraints,
                    candidates=(local_candidate(fixture),)))
                labels = {fixed.identity.session_id: UnitLabels("older", unit="older"),
                          movable.identity.session_id: UnitLabels("movable", unit="movable")}
                with mock.patch.object(gate, "priority_labels", return_value=labels):
                    self.assertIsInstance(fixture.connect(fixed).replies()[-1], wire.Wait)
                    self.assertIsInstance(fixture.connect(movable).replies()[-1], wire.PlacementWait)
                    view, = fixture.connect(wire.ViewRequest()).replies()
                    self.assertIsInstance(view, wire.ViewReply)
                    assert isinstance(view, wire.ViewReply)
                    self.assertEqual([row.pid for row in view.reading.view.waiting],
                                     [fixed.owner.pid, movable.owner.pid])
                    places = dict(fixture.server.run_places)
                    if prioritize_movable:
                        self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                            "movable-first", "movable", "the user", "fixture-director")), [wire.Ok()])
                    view, = fixture.connect(wire.ViewRequest()).replies()
                    assert isinstance(view, wire.ViewReply)
                    expected = ([movable.owner.pid, fixed.owner.pid] if prioritize_movable
                                else [fixed.owner.pid, movable.owner.pid])
                    self.assertEqual([row.pid for row in view.reading.view.waiting], expected)
                    self.assertEqual(fixture.server.run_places, places)
                    fixture.free(60 * GIB)
                    write_pressure_reading(fixture.root / "pressure")
                    _ = fixture.run_pass()
                    self.assertEqual(set(fixture.server.reservations),
                                     {movable.episode if prioritize_movable else fixed.episode})
                    if prioritize_movable:
                        granted, = fixture.connect(movable).replies()
                        self.assertIsInstance(granted, wire.LocalPlacementGranted)

    def test_token_readiness_blocks_local_memory_but_allows_remote_placement(self) -> None:
        cases = ((False, False, False, True), (True, False, False, True),
                 (False, True, False, True), (True, True, False, True),
                 (True, True, True, True), (True, True, True, False), (True, False, True, True))
        for remote_ready, local_first, unchecked, available_after_check in cases:
            with self.subTest(remote_ready=remote_ready, local_first=local_first,
                              unchecked=unchecked, available_after_check=available_after_check), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request, ALLOWED_HOST if remote_ready
                                      else placement.TemporaryExclusion("disk"))
                local = local_candidate(fixture)
                candidates = ((local, *request.constraints.candidates) if local_first
                              else (*request.constraints.candidates, local))
                prerequisite = (placement.TokenRequired(placement.TokenReadinessUnchecked())
                                if unchecked else placement.TokenRequired())
                request = replace(request, prerequisite=prerequisite, constraints=replace(
                    request.constraints, candidates=candidates))
                reply, = fixture.connect(request).replies()
                remote_selected = remote_ready and not (unchecked and local_first)
                self.assertIsInstance(reply, wire.Assignment if remote_selected else wire.PlacementWait)
                self.assertEqual(fixture.caller_reservations(), {})
                if remote_selected:
                    assert isinstance(reply, wire.Assignment)
                    self.assertEqual(reply.assignment.destination.provider, "mac")
                else:
                    confirmed = (placement.TokenHeld() if available_after_check else
                                 placement.TokenRequired(placement.TokenUnavailable()))
                    _ = fixture.connect(wire.PrerequisiteReport(
                        "token-checked", request.episode, request.owner, confirmed)).replies()
                    reply, = fixture.connect(request).replies()
                    if available_after_check:
                        self.assertIsInstance(reply, wire.LocalPlacementGranted)
                        self.assertEqual(set(fixture.server.reservations), {request.episode})
                    else:
                        self.assertIsInstance(reply, wire.Assignment)
                        assert isinstance(reply, wire.Assignment)
                        self.assertEqual(reply.assignment.destination.provider, "mac")
                        self.assertEqual(fixture.caller_reservations(), {})

    def test_released_local_grants_cannot_authorize_a_replayed_step(self) -> None:
        for ending in ("release", "cancel"):
            with self.subTest(ending=ending), PlacementFixture() as fixture:
                request = placement_request(fixture)
                request = replace(request, constraints=replace(request.constraints,
                    candidates=(local_candidate(fixture),)))
                granted, = fixture.connect(request).replies()
                self.assertIsInstance(granted, wire.LocalPlacementGranted)
                assert isinstance(granted, wire.LocalPlacementGranted)
                key = placement.attempt_key(granted.assignment.episode, granted.assignment.attempt)
                self.assertIsInstance(fixture.server.assignments[key].permission, placement.PermissionConsumed)
                if ending == "release":
                    fixture.release(granted.grant)
                else:
                    self.assertEqual(fixture.server.handle_request(wire.CancelPlacement(
                        "cancel-consumed-local", request.episode, request.owner, "the call was interrupted")), [wire.Ok()])
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.ClosedAssignment)
                self.assertEqual(fixture.caller_reservations(), {})
                replay, = fixture.connect(request).replies()
                self.assertNotIsInstance(replay, wire.LocalPlacementGranted)

    def test_restart_requires_token_confirmation_before_a_local_grant(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = placement_request(fixture)
            request = replace(request, prerequisite=placement.TokenHeld(), constraints=replace(
                request.constraints, candidates=(local_candidate(fixture),)))
            self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
            places = dict(fixture.server.run_places)
            fixture.restart()
            fixture.free(60 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
            self.assertEqual(fixture.caller_reservations(), {})
            self.assertEqual(fixture.server.run_places, places)
            _ = fixture.connect(wire.PrerequisiteReport(
                "token-reconfirmed", request.episode, request.owner, placement.TokenHeld())).replies()
            self.assertIsInstance(fixture.connect(request).replies()[-1], wire.LocalPlacementGranted)
            self.assertEqual(set(fixture.server.reservations), {request.episode})

    def test_remote_refusal_gets_one_local_reservation_for_the_replacement(self) -> None:
        cases = ((offload.DeclinedMacRun("disk", 0), True, False),
                 (offload.NoTestMatchedOnMac(0), True, True), (offload.LostMacRun(0), True, True),
                 (offload.LostMacRun(0), False, False))
        for result, consumed, authorized in cases:
            with self.subTest(result=type(result).__name__, consumed=consumed), PlacementFixture() as fixture:
                request = placement_request(fixture)
                request = replace(request, constraints=replace(request.constraints,
                    candidates=(*request.constraints.candidates, local_candidate(fixture))))
                _ = publish_candidate(fixture, request)
                remote = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, remote)
                if consumed:
                    self.assertIsInstance(fixture.connect(consume_permission("remote-permission", remote, claim)).replies()[-1],
                                          wire.FirstAuthorization)
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion("disk"))
                controller = offload.ProcessController()
                controller.attempt_evidence.provider = claim
                controller.attempt_evidence.cleanup = "confirmed"
                controller.attempt_evidence.launch_authorized = authorized
                adapter_request = offload.OffloadRequest(fixture.root, "hana", "call", offload.NO_RESULT_FILE, (),
                    owner="fixture-adapter", holder_session=claim_state.session_id(remote.session),
                    target=remote.destination.target, placement=offload.AssignedOffloadRequest(remote))
                with fixture.serving():
                    offload.report_placement_result(controller, adapter_request, result)
                fixture.clock.advance(0.01)
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion("disk"))
                granted, = fixture.connect(request).replies()
                self.assertIsInstance(granted, wire.LocalPlacementGranted)
                if not isinstance(granted, wire.LocalPlacementGranted):
                    self.fail(f"A settled refusal did not permit local admission: {granted!r}")
                self.assertNotEqual(granted.assignment.attempt, remote.attempt)
                self.assertEqual((granted.assignment.episode, granted.assignment.since), (request.episode, request.since))
                self.assertEqual(set(fixture.server.reservations), {request.episode})
                self.assertEqual(fixture.connect(request).replies(), [granted])
                view, = fixture.connect(wire.ViewRequest()).replies()
                assert isinstance(view, wire.ViewReply)
                local = [row for row in view.reading.view.running if row.pid == request.owner.pid]
                self.assertEqual(len(local), 1)
                self.assertEqual(local[0].promise, 8 * GIB)
                self.assertEqual(fixture.connect(consume_permission("late-old-consumption", remote, claim)).replies()[0].__class__,
                                 wire.DispatchStatus)

    def test_remote_selection_releases_only_its_unused_continuation_without_spacing(self) -> None:
        with PlacementFixture() as fixture:
            leaving_run, staying_run = fixture.run_owner(), fixture.run_owner()
            leaving_key = fixture.finish_step(fixture.request(run=leaving_run)).reservation
            staying_key = fixture.finish_step(fixture.request(run=staying_run)).reservation
            self.assertIn(leaving_key, fixture.server.slots)
            retained = fixture.server.slots[staying_key]
            spacing = records.NextStartAt(fixture.clock.now() + 60)
            fixture.server.spacing = spacing
            request = replace(placement_request(fixture), run=leaving_run)
            _ = publish_candidate(fixture, request)
            assigned = assignment_for(fixture, request)
            self.assertEqual(assigned.destination.provider, "mac")
            self.assertNotIn(leaving_key, fixture.server.slots)
            self.assertEqual(fixture.server.slots[staying_key], retained)
            self.assertEqual(fixture.server.spacing, spacing)
            self.assertEqual(fixture.caller_reservations(), {})

    def test_terminal_reports_close_the_exact_attempt_and_cannot_close_its_replacement(self) -> None:
        cases = (("busy", 0, False, False, False), ("blocked", 0, False, False, False),
                 ("disk", 0, False, False, False), ("unreachable", 180, False, False, False),
                 ("disk", 0, True, False, False), ("disk", 0, False, True, False),
                 ("busy", 0, True, False, True), ("busy", 0, False, True, True))
        for reason, delay, delayed_absence, stale_absence, foreign_claim in cases:
            with self.subTest(reason=reason, delayed_absence=delayed_absence,
                              stale_absence=stale_absence, foreign_claim=foreign_claim), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                original = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, original)
                if foreign_claim:
                    claim = replace(claim, claim_id="retained-claim", episode="previous-episode", attempt="previous-attempt")
                    retained = placement.AdoptedProviderClaim(claim, coordinator_holder(original), original.session,
                        original.destination.host, "host-generation-a")
                    _ = publish_candidate(fixture, request, claims=(retained,))
                else:
                    _ = fixture.connect(consume_permission("consume-original", original, claim)).replies()
                if not delayed_absence:
                    _ = publish_candidate(fixture, request)
                if stale_absence:
                    fixture.clock.advance(1)
                report = (wire.ClaimFailure("retained-claim-refused", original, reason, claim) if foreign_claim
                    else wire.ExecutionReport("prelaunch-refused", original, claim, placement.ExecutionNotStarted(),
                        placement.PrelaunchRefused(reason), placement.DispatchPathSettled()))
                reported_at = fixture.clock.now()
                _ = fixture.connect(report).replies()
                if stale_absence:
                    closed = fixture.server.assignments[placement.attempt_key(original.episode, original.attempt)]
                    self.assertIsInstance(closed.state, placement.ClosedAssignment)
                    self.assertEqual(closed.provider, claim)
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    fixture.clock.advance(1)
                    _ = publish_candidate(fixture, request)
                    _ = fixture.run_pass()
                if delayed_absence:
                    closed = fixture.server.assignments[placement.attempt_key(original.episode, original.attempt)]
                    self.assertIsInstance(closed.state, placement.ClosedAssignment)
                    self.assertEqual(closed.provider, claim)
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    fixture.restart()
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    self.assertEqual(fixture.server.assignments[placement.attempt_key(
                        original.episode, original.attempt)].provider, claim)
                    fixture.clock.advance(7)
                    _ = publish_candidate(fixture, request)
                    _ = fixture.run_pass()
                saved = fixture.server.assignments[placement.attempt_key(original.episode, original.attempt)]
                self.assertIsInstance(saved.state, placement.ClosedAssignment)
                self.assertEqual(saved.retry, placement.RetryAt(reported_at + delay))
                places = dict(fixture.server.run_places)
                fixture.restart()
                if delay:
                    _ = publish_candidate(fixture, request)
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    fixture.clock.advance(delay - 0.01)
                    _ = publish_candidate(fixture, request)
                    self.assertIsInstance(fixture.connect(request).replies()[-1], wire.PlacementWait)
                    fixture.clock.advance(0.01)
                _ = publish_candidate(fixture, request)
                replacement = assignment_for(fixture, request)
                self.assertNotEqual(replacement.attempt, original.attempt)
                self.assertEqual(replacement.since, original.since)
                self.assertEqual(fixture.server.run_places, places)
                _ = fixture.connect(replace(report, request_id="late-old-report")).replies()
                self.assertEqual(assignment_for(fixture, request), replacement)
                replacement_claim = adopt_assignment(fixture, request, replacement)
                self.assertIsInstance(fixture.connect(consume_permission(
                    "replacement-launch", replacement, replacement_claim)).replies()[-1], wire.FirstAuthorization)

    def test_final_command_results_never_retry_on_another_available_host(self) -> None:
        for status in (0, 17):
            with self.subTest(status=status), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                _ = fixture.connect(consume_permission("consume-completed", assignment, claim)).replies()
                _ = publish_candidate(fixture, request)
                _ = fixture.connect(wire.ExecutionReport("completed", assignment, claim,
                    placement.ExecutionNotStarted(), placement.AttemptCompleted(status),
                    placement.DispatchPathSettled())).replies()
                fixture.restart()
                reply, = fixture.connect(request).replies()
                self.assertIsInstance(reply, wire.DispatchStatus)
                assert isinstance(reply, wire.DispatchStatus)
                self.assertIsInstance(reply.state, placement.ClosedAssignment)
                self.assertIsInstance(reply.permission, placement.PermissionConsumed)
                self.assertEqual(len(fixture.server.assignments), 1)

    def test_an_empty_scan_cannot_close_a_pending_dispatch_before_or_after_restart(self) -> None:
        cases: tuple[tuple[str, placement.AttemptOutcome], ...] = tuple(
            (pending, outcome) for pending in ("claim", "start", "cleanup")
                      for outcome in (placement.PrelaunchRefused("disk"),
                                      placement.AttemptOutcomeUnavailable("the remote start is not settled")))
        cases += (("settled-loss-stale-evidence", placement.AttemptDeclined("lost")),)
        for pending, outcome in cases:
            with self.subTest(pending=pending, outcome=type(outcome).__name__), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                authorized, = fixture.connect(consume_permission("consume-pending", assignment, claim)).replies()
                self.assertIsInstance(authorized, wire.FirstAuthorization)
                _ = publish_candidate(fixture, request)
                if pending == "settled-loss-stale-evidence":
                    fixture.clock.advance(1)
                report = wire.ExecutionReport("empty-scan", assignment, claim, placement.ExecutionNotStarted(),
                    outcome, placement.DispatchPathSettled() if pending == "settled-loss-stale-evidence"
                    else placement.DispatchPathPending(pending))
                _ = fixture.connect(report).replies()
                for restart in (False, True):
                    if restart:
                        fixture.restart()
                    waiting, = fixture.connect(request).replies()
                    self.assertIsInstance(waiting, wire.PlacementWait)
                    saved = fixture.server.assignments[placement.attempt_key(assignment.episode, assignment.attempt)]
                    self.assertEqual(saved.assignment, assignment)
                    self.assertIsInstance(saved.state, placement.CurrentAssignment)
                    reply, = fixture.connect(consume_permission(f"repeat-{restart}", assignment, claim)).replies()
                    self.assertIsInstance(reply, wire.DispatchStatus)
                    assert isinstance(reply, wire.DispatchStatus)
                    self.assertIsInstance(reply.permission, placement.PermissionConsumed)
                _ = publish_candidate(fixture, request)
                _ = fixture.connect(replace(report, request_id="path-settled", outcome=placement.PrelaunchRefused("disk"),
                    path=placement.DispatchPathSettled())).replies()
                if pending == "settled-loss-stale-evidence":
                    fixture.clock.advance(180)
                    _ = publish_candidate(fixture, request)
                self.assertNotEqual(assignment_for(fixture, request).attempt, assignment.attempt)

    def test_verify_hands_its_exact_claim_to_a_child_and_keeps_the_coordinator_death_check(self) -> None:
        for ending in ("live", "coordinator-ended", "adapter-ended", "adapter-release-interrupted"):
            coordinator_ends = ending == "coordinator-ended"
            with self.subTest(ending=ending), PlacementFixture() as fixture:
                program = ("import subprocess,sys; child=subprocess.Popen(['sleep','300']); "
                           "print(child.pid,flush=True); sys.stdin.readline(); child.terminate(); child.wait(); "
                           "print('adapter-ended',flush=True); sys.stdin.read()")
                with subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as coordinator:
                    try:
                        if coordinator.stdout is None:
                            self.fail("The private coordinator has no output pipe")
                        output = cast(TextIO, coordinator.stdout)
                        self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0])
                        holder_pid = int(output.readline())
                        owner_start = records.process_start_time(coordinator.pid)
                        holder_start = records.process_start_time(holder_pid)
                        if owner_start is None or holder_start is None:
                            self.fail("The private process tree has no checked start identity")
                        fixture.proc.register(wire.Owner(coordinator.pid, owner_start))
                        fixture.proc.register(wire.Owner(holder_pid, holder_start))
                        write_process(fixture.proc.path, holder_pid, parent=coordinator.pid, start=holder_start)
                        request = placement_request(fixture)
                        request = replace(request, owner=placement.SubmittingOwner(
                            fixture.server.host, coordinator.pid, owner_start), run=wire.RunIdentity(coordinator.pid, owner_start))
                        _ = publish_candidate(fixture, request)
                        assignment = assignment_for(fixture, request)
                        paths = mac_test.StatePaths(fixture.root / "provider")
                        repository = fixture.root / "hana"
                        repository.mkdir()
                        _ = subprocess.run(["git", "init", "-q", str(repository)],
                            capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)
                        settings = fixture.root / "mac_test.conf"
                        _ = write_config_fixture(settings,
                            "offload=on\nlinux_only.hana=\nmac_skip.hana=\n", template="mac_test")
                        args = mac_test.CliArguments()
                        args.pid, args.worktree, args.what = coordinator.pid, str(repository), "nextest hana"
                        args.repo_root, args.package, args.settings = str(repository), "hana", str(settings)
                        args.target = "mac"
                        with mock.patch.object(mac_test, "start_claim_watcher"), \
                             mock.patch.object(mac_test, "target_claim_scope", return_value=assignment.destination.scope), \
                             redirect_stdout(io.StringIO()):
                            self.assertEqual(mac_test.command_claim_wait(args, paths,
                                session=assignment.session, assignment=assignment), 0)
                            reserved, = mac_test.read_claims(paths)
                            identity = claim_state.claim_identity(reserved.record)
                            args.pid, args.from_pid, args.remote = holder_pid, coordinator.pid, assignment.destination.host
                            args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
                            self.assertEqual(mac_test.command_claim(args, paths, watch_claim=False,
                                session=assignment.session, assignment=assignment), 0)
                        transferred, = mac_test.read_claims(paths)
                        self.assertEqual(claim_state.claim_identity(transferred.record), identity)
                        self.assertEqual(transferred.record["pid"], holder_pid)
                        self.assertNotEqual(holder_pid, request.owner.pid)
                        holder = placement.ClaimHolderIdentity(fixture.server.host, holder_pid, holder_start)
                        reference = placement.ProviderClaimReference(assignment.destination.provider,
                            assignment.destination.target, assignment.destination.scope,
                            identity.claim_id, identity.episode, identity.attempt)
                        adopted = placement.AdoptedProviderClaim(reference, holder, assignment.session,
                            assignment.destination.host, "host-generation-a")
                        _ = publish_candidate(fixture, request, claims=(adopted,))
                        before = transferred.path.read_bytes()
                        if coordinator_ends:
                            _ = coordinator.communicate("", timeout=TEST_HANG_GUARD_SECS)
                        _ = fixture.run_pass()
                        reply, = fixture.connect(wire.ConsumePermission("child-consumption", assignment,
                            reference, holder=holder)).replies()
                        self.assertIsInstance(reply, wire.DispatchStatus if coordinator_ends else wire.FirstAuthorization)
                        self.assertEqual(transferred.path.read_bytes(), before)
                        if not coordinator_ends:
                            saved = fixture.server.assignments[placement.attempt_key(assignment.episode, assignment.attempt)]
                            self.assertEqual(saved.holder, holder)
                        if ending in ("adapter-ended", "adapter-release-interrupted"):
                            key = placement.attempt_key(assignment.episode, assignment.attempt)
                            saved = fixture.server.assignments[key]
                            self.assertIsNone(coordinator.poll())
                            fixture.clock.advance(1)
                            _ = publish_candidate(fixture, request)
                            _ = fixture.run_pass()
                            waiting, = fixture.connect(request).replies()
                            self.assertIsInstance(waiting, wire.PlacementWait)
                            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                            assert coordinator.stdin is not None
                            _ = coordinator.stdin.write("end adapter\n")
                            coordinator.stdin.flush()
                            self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0])
                            self.assertEqual(output.readline(), "adapter-ended\n")
                            self.assertIsNone(records.process_start_time(holder_pid))
                            self.assertIsNone(coordinator.poll())
                            self.assertIsInstance(saved.permission, placement.PermissionConsumed)
                            assert isinstance(saved.permission, placement.PermissionConsumed)
                            evidence_key = fixture.server.host_evidence_key(request, assignment.destination)
                            stale = replace(fixture.provider_evidence[evidence_key],
                                            observed_at=saved.permission.consumed_at)
                            fixture.provider_evidence[evidence_key] = stale
                            fixture.server.publish_host_evidence(stale)
                            _ = fixture.run_pass()
                            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                            fixture.clock.advance(1)
                            _ = publish_candidate(fixture, request, claims=(adopted,))
                            _ = fixture.run_pass()
                            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                            pending = offload.pending_start_path(assignment.destination.target, identity)
                            pending.parent.mkdir(parents=True, exist_ok=True)
                            _ = pending.write_text("pending startup\n")
                            _ = publish_candidate(fixture, request)
                            _ = fixture.run_pass()
                            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                            if ending == "adapter-release-interrupted":
                                command = offload.prepare_start_fence_command(
                                    identity, "mac", scope_key=transferred.path.stem)
                                prepared = subprocess.run(["bash", "-c", command],
                                    capture_output=True, text=True, check=False, timeout=5)
                                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                                release_program = """
import os
from pathlib import Path
import signal
import subprocess
import sys
from unittest import mock
from scripts.mac_test import mac_test
paths = mac_test.StatePaths(Path(sys.argv[1]))
args = mac_test.CliArguments()
args.pid, args.target, args.settings = int(sys.argv[2]), "mac", sys.argv[3]
args.claim_id, args.episode, args.attempt = sys.argv[4:7]
claim = Path(sys.argv[7])
real_run, real_unlink = subprocess.run, Path.unlink

def private_transport(arguments, **options):
    return real_run(["bash", "-c", arguments[-1]], capture_output=True,
                    text=True, check=False, timeout=5)

def kill_before_claim_unlink(path, missing_ok=False):
    if path == claim:
        os.kill(os.getpid(), signal.SIGKILL)
    real_unlink(path, missing_ok=missing_ok)

with mock.patch.object(subprocess, "run", side_effect=private_transport), \
     mock.patch.object(Path, "unlink", kill_before_claim_unlink):
    raise SystemExit(mac_test.command_release(args, paths))
"""
                                interrupted = subprocess.run([sys.executable, "-c", release_program,
                                    str(paths.directory), str(args.pid), str(settings), identity.claim_id,
                                    identity.episode, identity.attempt, str(transferred.path)],
                                    capture_output=True, text=True, check=False, timeout=5)
                                self.assertEqual(interrupted.returncode, -signal.SIGKILL,
                                                 interrupted.stdout + interrupted.stderr)
                                self.assertFalse(pending.exists())
                                self.assertEqual(transferred.path.read_bytes(), before)
                                fence = fixture.root / offload.start_fence_relative(
                                    identity, "mac", scope_key=transferred.path.stem).with_suffix(".lock")
                                self.assertFalse(fence.exists())
                                _ = publish_candidate(fixture, request, claims=(adopted,))
                                _ = fixture.run_pass()
                                self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                                with mock.patch.object(subprocess, "run", side_effect=AssertionError(
                                        "record-free cleanup must not start SSH")):
                                    self.assertEqual(mac_test.command_release(args, paths), 0)
                                self.assertIsInstance(mac_test.read_run(transferred.path), mac_test.NoRun)
                                _ = publish_candidate(fixture, request)
                            else:
                                pending.unlink()
                            _ = fixture.run_pass()
                            closed = fixture.server.assignments[key]
                            self.assertIsInstance(closed.state, placement.ClosedAssignment)
                            self.assertEqual(closed.terminal_report, placement.TerminalAttemptReport(
                                placement.AttemptDeclined("lost"), placement.DispatchPathSettled(), fixture.clock.now()))
                            self.assertIsNone(coordinator.poll())
                            self.assertEqual(len(fixture.server.assignments), 1)
                            fixture.clock.advance(180)
                            _ = publish_candidate(fixture, request)
                            replacement = assignment_for(fixture, request)
                            self.assertNotEqual(replacement.attempt, assignment.attempt)
                            self.assertEqual(replacement.owner, assignment.owner)
                            self.assertEqual(replacement.since, assignment.since)
                    finally:
                        if coordinator.poll() is None:
                            _ = coordinator.communicate("", timeout=TEST_HANG_GUARD_SECS)

    def test_reader_assigns_supported_profile_steps_and_keeps_unknown_steps_waiting(self) -> None:
        for step in ("nextest", "clippy", "mend-check", "check", "doc", "build", "unknown"):
            with self.subTest(step=step), PlacementFixture() as fixture:
                repository = fixture.root / "hana"
                (repository / ".git").mkdir(parents=True)
                _ = (fixture.root / "build_targets.conf").write_text(
                    "targets=box\nkind.box=ssh\nplatform.box=linux\nsettings.box=box.conf\n")
                _ = write_config_fixture(fixture.root / "box.conf",
                    "offload=on\nhost=fixture-box\nkinds=test,lint,check,doc,build\nslots=2\n", template="mac_test")
                request = placement_request(fixture)
                assert isinstance(request.profile, records.StepProfile)
                destination = placement.PlacementDestination("ssh", "box", "fixture-box", RepositoryScope("hana"))
                request = replace(request, profile=replace(request.profile, step=step), constraints=replace(
                    request.constraints, repository=str(repository), candidates=(placement.PlacementCandidate(destination),)))
                key = fixture.server.host_evidence_key(request, destination)
                fixture.server.host_requests[key] = request

                def ssh_reply(arguments: Sequence[str],
                              deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
                    self.assertEqual((arguments[0], arguments[-2], arguments[-1]), ("ssh", "fixture-box", "true"))
                    self.assertIsInstance(deadline, offload.CommandTimeLimit)
                    return offload.CommandOutput(0, "", "")

                with mock.patch.object(offload.ProcessController, "capture", side_effect=ssh_reply):
                    evidence = fixture.read_host_evidence(key)
                fixture.server.publish_host_evidence(evidence)
                if step == "unknown":
                    self.assertIsInstance(evidence, placement.HostEvidenceUnavailable)
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.PlacementWait)
                else:
                    self.assertEqual(assignment_for(fixture, request).destination, destination)

    def test_a_lost_or_delivered_authorization_never_replays_before_or_after_restart(self) -> None:
        for response in ("lost", "delivered", "delivered_before_next_pass"):
            for restart in (False, True):
                with self.subTest(response=response, restart=restart), PlacementFixture() as fixture:
                    fixture.configure(replay_retention_seconds=1)
                    request = placement_request(fixture)
                    _ = publish_candidate(fixture, request)
                    assignment = assignment_for(fixture, request)
                    self.assertEqual(assignment.destination, request.constraints.candidates[0].destination)
                    self.assertEqual((assignment.episode, assignment.source_key, assignment.owner, assignment.since),
                                     (request.episode, request.constraints.source_key, request.owner, request.since))
                    fixture.restart()
                    self.assertEqual(assignment_for(fixture, request), assignment)
                    claim = adopt_assignment(fixture, request, assignment)
                    consume = replace(consume_permission("consume-first", assignment, claim),
                                      code_version="consume-caller-version")
                    fixture.fault.seen.clear()

                    def durable_authorization(reply: wire.Reply) -> wire.Reply:
                        if isinstance(reply, wire.FirstAuthorization):
                            state = service.read_state(fixture.state_dir)
                            key = placement.attempt_key(assignment.episode, assignment.attempt)
                            self.assertIsInstance(state.assignments[key].permission, placement.PermissionConsumed)
                            start = records.process_start_time(os.getpid())
                            self.assertIsNotNone(start)
                            self.assertEqual(state.caller_versions[records.run_owner(os.getpid(), str(start))],
                                             consume.code_version)
                            self.assertEqual(fixture.fault.seen.count("after_state_write"), 1)
                        return reply

                    fixture.server.reply_filter = durable_authorization
                    if response == "lost":
                        peer = fixture.connect(consume)
                        for _ in range(4):
                            fixture.server.run_once(0)
                        peer.close()
                    elif response == "delivered":
                        reply, = fixture.connect(consume).replies()
                        self.assertEqual(reply, wire.FirstAuthorization(assignment, claim))
                    else:
                        from scripts.build_queue import admission_client

                        queued, finish = threading.Event(), threading.Event()
                        run_pass = fixture.server.run_pass

                        def hold_next_pass() -> gate.PassResult | gate.UnusableAdmissionConfiguration:
                            if queued.is_set() and not finish.wait(TEST_HANG_GUARD_SECS):
                                raise AssertionError("the test did not release the independent admission pass")
                            return run_pass()

                        def queued_authorization(reply: wire.Reply) -> wire.Reply:
                            reply = durable_authorization(reply)
                            if isinstance(reply, wire.FirstAuthorization):
                                queued.set()
                                fixture.server.next_pass = fixture.clock.monotonic()
                            return reply

                        fixture.server.reply_filter = queued_authorization
                        with mock.patch.object(fixture.server, "run_pass", side_effect=hold_next_pass), \
                             mock.patch.object(admission_client, "LOADED_ADMISSION_CODE_VERSION", consume.code_version), \
                             fixture.serving():
                            try:
                                self.assertEqual(admission_client.consume_permission(consume),
                                                 wire.FirstAuthorization(assignment, claim))
                            finally:
                                finish.set()
                    if restart:
                        fixture.clock.advance(2)
                        _ = fixture.run_pass()
                        fixture.restart()
                    for request_id in (consume.request_id, "another-transport-request"):
                        reply, = fixture.connect(replace(consume, request_id=request_id)).replies()
                        self.assertIsInstance(reply, wire.DispatchStatus)
                        assert isinstance(reply, wire.DispatchStatus)
                        self.assertEqual(reply.assignment, assignment)
                        self.assertIsInstance(reply.permission, placement.PermissionConsumed)

    def test_closed_unknown_and_changed_assignments_never_receive_authorization(self) -> None:
        for case in ("closed", "unknown", "source", "owner", "claim"):
            with self.subTest(case=case), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                original = assignment
                if case == "closed":
                    _ = fixture.server.handle_request(wire.ClaimFailure("claim-refused", assignment, "busy"))
                    replacement = assignment_for(fixture, request)
                    self.assertNotEqual(replacement.attempt, assignment.attempt)
                    self.assertEqual(replacement.since, assignment.since)
                elif case == "unknown":
                    assignment = replace(assignment, attempt="unknown-old-attempt")
                    claim = replace(claim, attempt=assignment.attempt)
                elif case == "source":
                    assignment = replace(assignment, source_key=placement.SourceKeyKnown("obsolete-source"))
                elif case == "owner":
                    assignment = replace(assignment, owner=replace(assignment.owner, host="another-coordinator"))
                elif case == "claim":
                    claim = replace(claim, claim_id="another-claim")
                for restart in (False, True):
                    if restart:
                        fixture.restart()
                    reply, = fixture.server.handle_request(consume_permission(f"consume-{restart}", assignment, claim))
                    self.assertIsInstance(reply, wire.DispatchStatus)
                if case in ("source", "owner", "claim") or case.startswith("generation-"):
                    claim = (replace(claim, claim_id="claim-a") if case.startswith("generation-")
                             else adopt_assignment(fixture, request, original))
                    reply, = fixture.server.handle_request(consume_permission("valid-consumption", original, claim))
                    self.assertEqual(reply, wire.FirstAuthorization(original, claim))

    def test_session_evidence_cannot_transfer_a_holders_exemption(self) -> None:
        with PlacementFixture() as fixture:
            holder = placement_request(fixture, session=OwningSession("holder"))
            stranger = placement_request(fixture, session=OwningSession("stranger"))
            unknown = placement_request(fixture, session=NoOwningSession())
            _ = publish_candidate(fixture, holder)
            assigned = assignment_for(fixture, holder)
            for request in (stranger, unknown):
                with self.subTest(session=request.constraints.session):
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.PlacementWait)
                    _ = publish_candidate(fixture, request, placement.TemporaryExclusion("blocked"))
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.PlacementWait)
                    assert isinstance(reply, wire.PlacementWait)
                    self.assertEqual(reply.eligibility[0].eligibility, placement.TemporaryExclusion("blocked"))
            self.assertEqual(assignment_for(fixture, holder), assigned)

    def test_execution_hosts_keep_equal_pids_separate_and_never_become_owner_watches(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            with mock.patch.object(fixture.proc, "pidfd", wraps=fixture.proc.pidfd) as watched:
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                reply, = fixture.server.handle_request(consume_permission("consume", assignment, claim))
                self.assertIsInstance(reply, wire.FirstAuthorization)
                executions = tuple(placement.ProcessExecution(host, "generation-a", 700001, "equal-start")
                                   for host in ("fixture-mac", "second-host"))
                self.assertEqual(len(set(executions)), 2)
                for execution in executions:
                    _ = fixture.server.handle_request(wire.ExecutionReport(
                        f"report-{execution.host}", assignment, claim, placement.ObservedExecution(execution)))
                fixture.restart()
            self.assertTrue(watched.call_args_list)
            self.assertEqual({call.args[0] for call in watched.call_args_list}, {request.owner.pid})

    def test_blocked_remote_evidence_keeps_socket_requests_fast_and_retains_the_claim(self) -> None:
        with PlacementFixture() as fixture:
            fixture.configure(waiting_pass_seconds=0.05)
            _ = write_config_fixture(fixture.root / "mac_test.conf", "probe_timeout_s=0.05\n", template="mac_test")
            _ = fixture.run_pass()
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assignment)
            paths = mac_test.StatePaths(fixture.root / "provider")
            paths.directory.mkdir()
            provider = paths.run
            record = {"claim_id": claim.claim_id, "episode": claim.episode, "attempt": claim.attempt,
                      "submitting_session": "submitting-session", "pid": request.owner.pid,
                      "proc_start": request.owner.start_time, "remote_host": assignment.destination.host,
                      "what": "hana tests", "worktree": str(fixture.root), "since": "2026-11-01T12:00:00+00:00"}
            _ = provider.write_text(json.dumps(record))
            before = provider.read_bytes()
            local = fixture.request()
            granted = only_admitted(fixture.server.handle_request(local))
            entered, finish = threading.Event(), threading.Event()
            read_started: list[float] = []

            def blocked_probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                read_started.append(fixture.clock.now())
                entered.set()
                if not finish.wait(5):
                    raise AssertionError("the test did not release the blocked probe")
                return placement.FreshHostEvidence(key, fixture.clock.now(), "host-generation-a", placement.AllowedHost())

            fixture.server.host_probe = blocked_probe
            key = fixture.server.host_evidence_key(request, assignment.destination)
            fixture.clock.advance(0.09)
            self.assertIsInstance(fixture.server.host_evidence(key), placement.FreshHostEvidence)
            fixture.clock.advance(0.02)
            self.assertIsInstance(fixture.server.host_evidence(key), placement.StaleHostEvidence)
            waiter = placement_request(fixture)
            try:
                reply, = fixture.connect(waiter).replies()
                self.assertIsInstance(reply, wire.PlacementWait)
                self.assertTrue(entered.wait(1), "the background probe did not start")
                fixture.allow_next_start()
                for operation in (wire.ViewRequest(), wire.ReleaseRequest("release-local", granted.reservation, 0),
                                  fixture.request()):
                    started = time.monotonic()
                    replies = fixture.connect(operation).replies()
                    self.assertLess(time.monotonic() - started, 1, type(operation).__name__)
                    self.assertTrue(replies)
                    self.assertIsInstance(replies[-1], wire.ViewReply if isinstance(operation, wire.ViewRequest)
                                          else wire.Ok if isinstance(operation, wire.ReleaseRequest) else wire.Admitted)
                self.assertIsInstance(fixture.server.host_evidence(key), placement.StaleHostEvidence)
                self.assertEqual(provider.read_bytes(), before)
                self.assertEqual([item.record["claim_id"] for item in mac_test.live_claims(paths)], [claim.claim_id])
                self.assertEqual(assignment_for(fixture, request), assignment)
            finally:
                reader = fixture.server.host_readers[key]
                reader.close()
                fixture.clock.advance(1)
                finish.set()
                reader.thread.join(timeout=5)
                self.assertFalse(reader.thread.is_alive())
            observed = fixture.server.host_evidence(key)
            self.assertIsInstance(observed, placement.StaleHostEvidence)
            self.assertLessEqual(observed.observed_at, read_started[0])
