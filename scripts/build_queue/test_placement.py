"""Placement preserves attempt identity and grants one execution authorization."""
from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable, Sequence
import json
import threading
import time
import unittest
from unittest import mock
from typing import override

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_queue import admission_protocol as wire, admission_records as records, placement
from scripts.build_queue.admission_test_support import AdmissionFixture, ObservedAdmissionServer, only_admitted
from scripts.mac_test.claim_state import RepositoryScope
from scripts.mac_test import claim_state, mac_test, offload
from scripts.lint.memory_test_support import write_config_fixture


SUBMITTING_SESSION = OwningSession("submitting-session")
ALLOWED_HOST = placement.AllowedHost()


class PlacementFixture(AdmissionFixture):
    """Supply private provider evidence across each service restart."""

    read_host_evidence: Callable[[placement.HostEvidenceKey], placement.HostEvidence]

    def __init__(self) -> None:
        self.provider_evidence: dict[placement.HostEvidenceKey, placement.HostEvidence] = {}
        super().__init__()

    def read_provider(self, key: placement.HostEvidenceKey) -> placement.HostEvidence:
        return self.provider_evidence.get(key, placement.HostEvidenceUnavailable(key, self.clock.now(), "not read"))

    @override
    def new_server(self, *, start: bool = True) -> ObservedAdmissionServer:
        server = super().new_server(start=False)
        self.read_host_evidence = server.host_probe
        server.host_probe = self.read_provider
        if start:
            server.start()
        return server


def placement_request(fixture: AdmissionFixture, *, session: placement.SubmittingSession = SUBMITTING_SESSION,
                      since: float | None = None) -> wire.PlacementRequest:
    local = fixture.request(since=since)
    destination = placement.PlacementDestination("mac", "mac", "fixture-mac", RepositoryScope("hana"))
    constraints = placement.PlacementConstraints("source-a", ("cargo", "nextest", "run"), "hana", "hana", session,
                                                 (placement.PlacementCandidate(destination),))
    owner = placement.SubmittingOwner(fixture.server.host, local.owner.pid, local.owner.start_time)
    return wire.PlacementRequest(local.episode, owner, local.since, local.profile, constraints,
                                 code_version=local.code_version)


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
    adopted = placement.AdoptedProviderClaim(reference, assignment.owner, assignment.session,
                                             destination.host, "host-generation-a")
    _ = publish_candidate(fixture, request, claims=(adopted,))
    return reference


class PlacementTests(unittest.TestCase):
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
        for response in ("lost", "delivered"):
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
                    consume = wire.ConsumePermission("consume-first", assignment, claim)
                    if response == "lost":
                        peer = fixture.connect(consume)
                        for _ in range(4):
                            fixture.server.run_once(0)
                        peer.close()
                    else:
                        reply, = fixture.connect(consume).replies()
                        self.assertEqual(reply, wire.FirstAuthorization(assignment, claim))
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
        for case in ("closed", "unknown", "source", "owner", "claim", "generation-old-last", "generation-old-first",
                     "generation-unversioned"):
            with self.subTest(case=case), PlacementFixture() as fixture:
                request = placement_request(fixture)
                if case.startswith("generation-"):
                    target = "box" if case == "generation-unversioned" else "aws"
                    settings = f"{target}_target.conf"
                    _ = (fixture.root / "build_targets.conf").write_text(
                        f"targets={target}\nkind.{target}=ssh\nplatform.{target}=linux\nsettings.{target}={settings}\n")
                    _ = write_config_fixture(fixture.root / settings, "offload=off\n", template="mac_test")
                    destination = replace(request.constraints.candidates[0].destination, provider="ssh", target=target)
                    request = replace(request, constraints=replace(request.constraints,
                        candidates=(placement.PlacementCandidate(destination),)))
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
                    assignment = replace(assignment, source_key="obsolete-source")
                elif case == "owner":
                    assignment = replace(assignment, owner=replace(assignment.owner, host="another-coordinator"))
                elif case == "claim":
                    claim = replace(claim, claim_id="another-claim")
                else:
                    paths = claim_state.state_paths(assignment.destination.target)
                    paths.claims.mkdir(parents=True)
                    _ = (paths.directory / "controller.json").write_text('{"generation":"host-generation-a"}')
                    current_generation = "" if case == "generation-unversioned" else "host-generation-a"
                    for name, generation in (("a", current_generation), ("z", "obsolete-generation")):
                        record = {"claim_id": f"claim-{name}", "episode": assignment.episode, "attempt": assignment.attempt,
                                  "submitting_session": "submitting-session", "pid": assignment.owner.pid,
                                  "proc_start": assignment.owner.start_time, "remote_host": assignment.destination.host,
                                  "repo_name": "hana", "repo_key": "", "what": "hana tests",
                                  "worktree": str(fixture.root), "since": "2026-11-01T12:00:00+00:00"}
                        if generation:
                            record["instance_generation"] = generation
                        filename = {"a": "z", "z": "a"}[name] if case == "generation-old-first" else name
                        _ = (paths.claims / f"{filename}.json").write_text(json.dumps(record))
                    key = fixture.server.host_evidence_key(request, assignment.destination)
                    fixture.server.host_requests[key] = request
                    evidence = fixture.read_host_evidence(key)
                    fixture.provider_evidence[key] = evidence
                    fixture.server.publish_host_evidence(evidence)
                    claim = replace(claim, claim_id="claim-z")
                for restart in (False, True):
                    if restart:
                        fixture.restart()
                    reply, = fixture.server.handle_request(wire.ConsumePermission(f"consume-{restart}", assignment, claim))
                    self.assertIsInstance(reply, wire.DispatchStatus)
                if case in ("source", "owner", "claim") or case.startswith("generation-"):
                    claim = (replace(claim, claim_id="claim-a") if case.startswith("generation-")
                             else adopt_assignment(fixture, request, original))
                    reply, = fixture.server.handle_request(wire.ConsumePermission("valid-consumption", original, claim))
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
                reply, = fixture.server.handle_request(wire.ConsumePermission("consume", assignment, claim))
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

            def blocked_probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not finish.wait(5):
                    raise AssertionError("the test did not release the blocked probe")
                return placement.HostEvidenceUnavailable(key, fixture.clock.now(), "fixture probe ended")

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
                finish.set()
