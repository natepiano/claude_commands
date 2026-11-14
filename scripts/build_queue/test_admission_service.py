"""In-process service decisions, persistence and owner events."""
from __future__ import annotations

from scripts.lib import test_phase

from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue.admission_protocol_v1_fixture import parse_reply as decode_legacy
from scripts.build_queue.scope_activity import LiveBuildRun, NoLiveBuildRun, ScopeActivityReader

from scripts.lint.memory_test_support import admission_configuration_fixture, admission_policy_fixture, fake_memory_readings, write_pressure_reading

from collections.abc import Callable, Generator
from contextlib import contextmanager, redirect_stderr
from dataclasses import replace
from datetime import datetime
import io
import json
import math
import errno
import os
from pathlib import Path
import stat
import select
import signal
import socket
import sys
import tempfile
import threading
import time
import subprocess
import unittest
from typing import cast, override
from unittest import mock

from scripts.lint.memory_test_support import write_config_fixture, commit_config_fixture
from scripts.lint import memory_admit as gate
from scripts.accounts import outage_alerts

from scripts.buildlog import store
from scripts.buildlog import record as buildlog_record

from scripts.lib import conf
from scripts.lib import inotify, test_hang_guard
from scripts.lib.user_zone import user_zone
from scripts.lib.atomic_file import write_text_atomic
from scripts.message import session_send
from scripts.message.session_send_test_support import forbid_real_sender
from scripts.message.roster import UnitLabels
from scripts.lint.build_shape import build_shape
from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_service as service
from scripts.build_queue import admission_code, queue_snapshot, queue_view, queue_card, queue_evidence, head_facts
from scripts.build_queue import admission_records as records, progress_profile as progress, step_estimator
from scripts.build_queue.queue_lines import plain
from zoneinfo import ZoneInfo
from scripts.build_queue.queue_fixture import write_memory_config, write_process
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, InjectedCrash, caller_admission, only_admitted


def setUpModule() -> None:
    forbid_real_sender()


class ImportTests(unittest.TestCase):
    def test_clean_import_omits_cli_and_reading_alert_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "HOME": directory, "BUILDLOG_DIR": directory}
            result = subprocess.run(
                [sys.executable, "-c", "import sys; from scripts.build_queue import admission_service; "
                 + "assert not {'argparse', 'scripts.message.send_budget', 'scripts.message.send_result', "
                 + "'scripts.message.session_send'}.intersection(sys.modules)"],
                cwd=Path(__file__).resolve().parents[2], env=environment,
                capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class LocalHoldPlacementTests(unittest.TestCase):
    def test_close_between_the_loop_check_and_clear_does_not_lose_its_wake(self) -> None:
        from queue import Queue
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            entered, release, clearing, allow_clear = (threading.Event() for _ in range(4))
            outcomes: Queue[str] = Queue()

            class ObservedWorker(service.HostEvidenceWorker):
                @override
                def _read(self) -> None:
                    try:
                        super()._read()
                    finally:
                        outcomes.put("ended")

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the probe")
                return placement.HostEvidenceUnavailable(key, fixture.clock.now(), "private probe")

            request = placement_request(fixture)
            key = fixture.server.host_evidence_key(request, request.constraints.candidates[0].destination)
            worker = ObservedWorker(key, probe, fixture.clock, fixture.server.reading_events, 60)
            try:
                self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                original_clear = worker.requested.clear
                original_wait = worker.requested.wait

                def clear() -> None:
                    clearing.set()
                    if not allow_clear.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                        raise AssertionError("the test did not permit the event clear")
                    original_clear()

                def wait(timeout: float | None = None) -> bool:
                    if worker.stopped.is_set():
                        outcomes.put("waited after close")
                        return True
                    return original_wait(timeout)

                with mock.patch.object(worker.requested, "clear", side_effect=clear), \
                     mock.patch.object(worker.requested, "wait", side_effect=wait):
                    worker.remove(key)
                    release.set()
                    self.assertTrue(clearing.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                    worker.close()
                    allow_clear.set()
                    self.assertEqual(outcomes.get(timeout=test_hang_guard.TEST_HANG_GUARD_SECS), "ended")
                    worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                    self.assertFalse(worker.thread.is_alive())
            finally:
                worker.close()
                release.set()
                allow_clear.set()
                worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)

    def test_an_empty_probe_schedule_reuses_its_live_worker_for_a_new_request(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            entered, release, empty_wait, published = (threading.Event() for _ in range(4))
            delays: list[float | None] = []

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the probe")
                return placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())

            fixture.server.host_probe = probe
            request = placement_request(fixture)
            try:
                _ = fixture.server.handle_request(request)
                self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                key, = fixture.server.host_readers
                worker = fixture.server.host_readers[key].worker
                original_wait = worker.requested.wait
                original_write = os.eventfd_write

                def wait(timeout: float | None = None) -> bool:
                    delays.append(timeout)
                    empty_wait.set()
                    return original_wait(timeout)

                def notify(fd: int, value: int) -> None:
                    original_write(fd, value)
                    if threading.current_thread() is worker.thread:
                        published.set()

                with mock.patch.object(worker.requested, "wait", side_effect=wait), \
                     mock.patch.object(os, "eventfd_write", side_effect=notify):
                    _ = fixture.server.handle_request(wire.CancelPlacement(
                        "cancel", request.episode, request.owner, "ended"))
                    with worker.lock:
                        self.assertEqual(worker.due, {})
                    release.set()
                    self.assertTrue(empty_wait.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                    self.assertIsNone(delays[0])
                    self.assertTrue(worker.thread.is_alive())
                    next_request = replace(request, episode="later", constraints=replace(
                        request.constraints, command=("cargo", "check")))
                    _ = fixture.server.handle_request(next_request)
                    next_key = fixture.server.host_evidence_key(next_request, key.destination)
                    self.assertIs(fixture.server.host_readers[next_key].worker, worker)
                    self.assertTrue(published.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                    self.assertIsInstance(fixture.server.host_evidence(next_key), placement.FreshHostEvidence)
                    self.assertTrue(worker.thread.is_alive())
            finally:
                for worker in fixture.server.host_targets.values():
                    worker.close()
                release.set()
                for worker in fixture.server.host_targets.values():
                    worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)

    def test_ended_requests_remove_their_probe_schedule_keys(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for,
            adopt_assignment, consume_permission,
        )

        for ending in ("cancelled", "completed"):
            with self.subTest(ending=ending), PlacementFixture() as fixture:
                entered, release = threading.Event(), threading.Event()

                def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                    entered.set()
                    if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                        raise AssertionError("the test did not release the probe")
                    return placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())

                fixture.server.host_probe = probe
                request = placement_request(fixture)
                try:
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.PlacementWait)
                    self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                    key, = fixture.server.host_readers
                    worker = fixture.server.host_readers[key].worker
                    if ending == "completed":
                        _ = publish_candidate(fixture, request)
                        assignment = assignment_for(fixture, request)
                        claim = adopt_assignment(fixture, request, assignment)
                        authorized, = fixture.server.handle_request(consume_permission("launch", assignment, claim))
                        self.assertIsInstance(authorized, wire.FirstAuthorization)
                        _ = fixture.server.handle_request(wire.ExecutionReport("done", assignment, claim,
                            placement.ExecutionNotStarted(), placement.AttemptCompleted(0), placement.DispatchPathSettled()))
                    else:
                        _ = fixture.server.handle_request(wire.CancelPlacement("cancel", request.episode, request.owner, "ended"))
                    self.assertNotIn(key, fixture.server.host_readers)
                    self.assertNotIn(key, fixture.server.host_requests)
                    with worker.lock:
                        self.assertNotIn(key, worker.due)
                    worker.close()
                    release.set()
                    worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                    self.assertFalse(worker.thread.is_alive())
                    with worker.lock:
                        self.assertNotIn(key, worker.due)
                        self.assertNotIn(key, worker.observations)
                finally:
                    for worker in fixture.server.host_targets.values():
                        worker.close()
                    release.set()
                    for worker in fixture.server.host_targets.values():
                        worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)

    def test_each_settings_file_controls_its_evidence_age(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            entered, release = threading.Event(), threading.Event()

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the probe")
                return placement.HostEvidenceUnavailable(key, fixture.clock.now(), "private probe")

            fixture.server.host_probe = probe
            keys: list[placement.HostEvidenceKey] = []
            try:
                for name, timeout in (("long", 10), ("short", 1)):
                    path = fixture.root / (name + ".conf")
                    _ = write_config_fixture(path, f"probe_timeout_s={timeout}\n", template="mac_test")
                    request = placement_request(fixture)
                    candidate = request.constraints.candidates[0]
                    request = replace(request, episode=name, constraints=replace(request.constraints,
                        candidates=(replace(candidate, settings=placement.ExplicitProviderSettings(path)),)))
                    _ = fixture.server.handle_request(request)
                    keys.append(fixture.server.host_evidence_key(request, candidate.destination))
                self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                self.assertIs(fixture.server.host_readers[keys[0]].worker, fixture.server.host_readers[keys[1]].worker)
                for key in keys:
                    fixture.server.publish_host_evidence(placement.FreshHostEvidence(
                        key, fixture.clock.now(), "", placement.AllowedHost()))
                config = fixture.server.last_usable_configuration
                assert isinstance(config, records.MemoryConfig)
                fixture.clock.advance(config.waiting_pass_seconds + 2)
                self.assertIsInstance(fixture.server.host_evidence(keys[0]), placement.FreshHostEvidence)
                self.assertIsInstance(fixture.server.host_evidence(keys[1]), placement.StaleHostEvidence)
            finally:
                for worker in fixture.server.host_targets.values():
                    worker.close()
                release.set()
                for worker in fixture.server.host_targets.values():
                    worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)

    def test_failed_execution_probes_start_the_provider_backoff(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request
        from scripts.build_targets import build_targets, target_config
        from scripts.mac_test import claim_state, eligibility, mac_test

        for failure in (OSError("ssh missing"), subprocess.TimeoutExpired("ssh", 1),
                        subprocess.CompletedProcess(["ssh"], 255, "", "unreachable")):
            with self.subTest(failure=failure), AdmissionFixture() as fixture:
                settings = fixture.root / "linux.conf"
                _ = write_config_fixture(settings, "probe_timeout_s=1\nunreachable_backoff_s=30\n", template="mac_test")
                target = target_config.SshTarget("linux", "linux", settings)
                request = placement_request(fixture)
                destination = replace(request.constraints.candidates[0].destination,
                                      provider="ssh", target="linux", host="private-linux")
                request = replace(request, constraints=replace(request.constraints, candidates=(
                    placement.PlacementCandidate(destination, settings=placement.ExplicitProviderSettings(settings)),)))
                paths = claim_state.StatePaths(fixture.root / "claims")
                record: claim_state.RemoteJobClaim = {"claim_id": "retained", "episode": "old", "attempt": "old-attempt",
                    "submitting_session": "", "pid": request.owner.pid, "proc_start": request.owner.start_time,
                    "remote_host": destination.host, "what": "busy claim", "worktree": str(fixture.root), "since": "private"}
                held = claim_state.HeldClaim(destination.scope, paths.run, record)
                with mock.patch.object(target_config, "read_targets", return_value=(target,)), \
                     mock.patch.object(build_targets, "ssh_target_eligibility", return_value=target_config.ExcludedTarget(target, "busy")), \
                     mock.patch.object(claim_state, "state_paths", return_value=paths), \
                     mock.patch.object(claim_state, "read_claims", return_value=(held,)), \
                     mock.patch.object(claim_state, "target_claim_scope", return_value=destination.scope), \
                     mock.patch.object(mac_test, "run_is_live", return_value=False), \
                     mock.patch.object(eligibility, "repository_identity", return_value=eligibility.KnownRepository("hana")), \
                     mock.patch.object(subprocess, "run") as probe:
                    if isinstance(failure, BaseException):
                        probe.side_effect = failure
                    else:
                        probe.return_value = failure
                    key = fixture.server.host_evidence_key(request, destination)
                    fixture.server.host_requests[key] = request
                    first = fixture.server.read_provider_evidence(key)
                    second = fixture.server.read_provider_evidence(key)
                    self.assertIsInstance(first, placement.FreshHostEvidence)
                    self.assertIsInstance(second, placement.FreshHostEvidence)
                    assert isinstance(second, placement.FreshHostEvidence)
                    self.assertEqual(len(second.claims), 1)
                    self.assertEqual(second.claims[0].reference.claim_id, "retained")
                    self.assertIsInstance(second.executions[0][1], placement.ExecutionEvidenceUnavailable)
                    self.assertEqual(probe.call_count, 1)
                    self.assertIsInstance(eligibility.read_backoff(time.time(), "linux"), eligibility.ActiveBackoff)

    def test_hanging_or_failing_target_readers_do_not_delay_a_view(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request, local_candidate

        for fails in (False, True):
            with self.subTest(fails=fails), AdmissionFixture() as fixture:
                entered = {name: threading.Event() for name in ("mac", "natedev")}
                release = threading.Event()
                fixture.server.background_local_evidence = True

                def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                    entered[key.destination.target].set()
                    if fails:
                        raise OSError("private provider unavailable")
                    if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                        raise AssertionError("the test did not release the hanging probe")
                    return placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())

                fixture.server.host_probe = probe
                request = placement_request(fixture)
                remote = request.constraints.candidates[0].destination
                local = local_candidate(fixture).destination
                request = replace(request, constraints=replace(request.constraints,
                    candidates=(*request.constraints.candidates, placement.PlacementCandidate(local))))
                second = replace(request, episode="second-placement",
                    constraints=replace(request.constraints, command=("cargo", "check")))
                try:
                    for item in (request, second):
                        reply, = fixture.server.handle_request(item)
                        self.assertIsInstance(reply, wire.PlacementWait)
                    for event in entered.values():
                        self.assertTrue(event.wait(2))
                    self.assertEqual(len(fixture.server.host_targets), 2)
                    remote_keys = [key for key in fixture.server.host_readers if key.destination == remote]
                    self.assertEqual(len(remote_keys), 2)
                    self.assertIs(fixture.server.host_readers[remote_keys[0]].thread,
                                  fixture.server.host_readers[remote_keys[1]].thread)
                    with fixture.serving():
                        reading = client.view()
                        self.assertIsInstance(reading, records.QueueRead)
                        self.assertFalse(release.is_set())
                    if fails:
                        key = fixture.server.host_evidence_key(request, remote)
                        evidence = fixture.server.host_evidence(key)
                        self.assertIsInstance(evidence, placement.HostEvidenceUnavailable)
                finally:
                    for worker in fixture.server.host_targets.values():
                        worker.close()
                    release.set()
                    for worker in fixture.server.host_targets.values():
                        worker.thread.join(timeout=5)
                        self.assertFalse(worker.thread.is_alive())

    def test_local_hold_evidence_uses_each_requesters_checked_lineage(self) -> None:
        from scripts.build_hold import build_hold
        from scripts.build_hold.owning_session import OwningSession
        from scripts.build_queue import placement
        from scripts.mac_test.claim_state import WholeMacScope

        for case, callers, holds in (
            ("kept", ((110, "1100", True),), "single"),
            ("descendant", ((111, "1110", True),), "single"),
            ("unrelated", ((112, "1120", False),), "single"),
            ("service-kept", ((112, "1120", False),), "service-kept"),
            ("reused", ((114, "1140", False),), "single"),
            ("unavailable", ((115, "1150", False),), "single"),
            ("ended-hold-invalid-config", ((112, "1120", False),), "single"),
            ("one-hold", ((111, "1110", False),), "blocking-second"),
            ("both-holds", ((111, "1110", True),), "kept-second"),
            ("cache-kept-first", ((111, "1110", True), (112, "1120", False)), "single"),
            ("cache-unrelated-first", ((112, "1120", False), (111, "1110", True)), "single"),
            ("cache-reused", ((111, "1110", True), (111, "1111", False)), "single"),
            ("completed", ((111, "1110", True),), "single"),
            ("cancelled", ((111, "1110", True),), "single"),
        ):
            with self.subTest(case=case), AdmissionFixture() as fixture:
                directory = fixture.root / "holders"
                directory.mkdir()
                keep = [{"pid": 110, "start_time": "1100"}]
                if holds == "service-kept":
                    keep.append({"pid": os.getpid(), "start_time": "1"})
                for key in ("holder", "second") if holds.endswith("second") else ("holder",):
                    _ = (directory / key).write_text(json.dumps({
                        "holder": key, "since": "2026-11-12T12:00:00+00:00",
                        "for": "private test", "release_eta": "unknown",
                        "keep": [] if key == "second" and holds == "blocking-second" else keep,
                    }))
                _ = (fixture.root / "build_targets.conf").write_text(
                    "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
                write_process(fixture.proc.path, os.getpid(), start="1")
                write_process(fixture.proc.path, 110, start="1100")
                destination = placement.PlacementDestination("local", "natedev", fixture.server.host, WholeMacScope())
                fixture.server.host_probe = fixture.server.read_provider_evidence
                with mock.patch.dict(os.environ, {"BUILD_HOLD_DIR": str(directory)}), \
                     mock.patch.object(build_hold, "PROC_ROOT", fixture.proc.path):
                    for index, (pid, start, admitted) in enumerate(callers):
                        with self.subTest(pid=pid, start=start):
                            owner = fixture.fixture_owner(pid, start)
                            write_process(fixture.proc.path, pid,
                                parent=110 if pid in (111, 114) and start != "1111" else 1,
                                start="1141" if case == "reused" else start)
                            if case == "unavailable":
                                (fixture.proc.path / str(pid) / "stat").unlink()
                            local = fixture.request(owner=owner, episode=f"{case}-{index}")
                            constraints = placement.PlacementConstraints(placement.SourceKeyKnown("private-source"),
                                local.argv, "fixture", "fixture", OwningSession("caller"),
                                (placement.PlacementCandidate(destination),))
                            request = wire.PlacementRequest(local.episode,
                                placement.SubmittingOwner(fixture.server.host, pid, start), local.since,
                                local.profile, constraints, identity=local.identity,
                                local=wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar,
                                    local.call, local.test_width, local.fresh_test), code_version=local.code_version)
                            key = fixture.server.host_evidence_key(request, destination)
                            self.assertIsInstance(fixture.server.host_evidence(key), placement.HostEvidenceUnavailable)
                            evidence = fixture.server.read_provider_evidence(key)
                            self.assertIsInstance(evidence, placement.FreshHostEvidence)
                            assert isinstance(evidence, placement.FreshHostEvidence)
                            self.assertEqual(evidence.eligibility,
                                placement.AllowedHost() if admitted else placement.TemporaryExclusion("blocked"))
                            fixture.allow_next_start()
                            reply, = fixture.server.handle_request(request)
                            if admitted:
                                self.assertIsInstance(reply, wire.LocalPlacementGranted)
                                assert isinstance(reply, wire.LocalPlacementGranted)
                                if case == "completed":
                                    fixture.release(reply.grant)
                                elif case == "cancelled":
                                    self.assertEqual(fixture.server.handle_request(wire.CancelPlacement(
                                        "cancel-local", request.episode, request.owner, "the caller cancelled")), [wire.Ok()])
                                if case in ("completed", "cancelled"):
                                    for retained in (fixture.server.host_readers, fixture.server.host_requests,
                                                     fixture.server.host_observations):
                                        self.assertNotIn(request.owner, [item.local_hold_owner for item in retained])
                            else:
                                self.assertIsInstance(reply, wire.PlacementWait)
                                assert isinstance(reply, wire.PlacementWait)
                                self.assertIsInstance(reply.wait, wire.PlacementResourceWait)
                                assert isinstance(reply.wait, wire.PlacementResourceWait)
                                self.assertEqual(reply.wait.holder,
                                    placement.ResourceHolderUnit("second" if holds == "blocking-second" else "holder"))
                                if case == "ended-hold-invalid-config":
                                    (directory / "holder").unlink()
                                    fixture.server.pending_configuration = gate.UnusableAdmissionConfiguration(
                                        "private invalid configuration")
                                    repeated, = fixture.server.handle_request(request)
                                    self.assertIsInstance(repeated, wire.PlacementWait)
                                    assert isinstance(repeated, wire.PlacementWait)
                                    self.assertIsInstance(repeated.wait, wire.PlacementResourceWait)
                                    assert isinstance(repeated.wait, wire.PlacementResourceWait)
                                    self.assertEqual(repeated.wait.holder, placement.ResourceHolderUnknown())


class FixedSystemReader:
    """Publish private evidence without an operating-system reader."""
    def __init__(self, latest: service.SystemEvidence) -> None:
        self.latest: service.SystemEvidence = latest
        self.plans: list[service.SystemReadingPlan] = []
        self.requests: int = 0

    def update(self, plan: service.SystemReadingPlan) -> None:
        self.plans.append(plan)

    def request_read(self) -> None:
        self.requests += 1

    def raise_failure(self) -> None:
        pass

    def close(self) -> None:
        pass


class ReferenceActivityReader(ScopeActivityReader):
    """Keep the original ancestry traversal as an independent test reference."""

    @override
    def run(self, pid: int) -> LiveBuildRun | NoLiveBuildRun:
        for ancestor in self.ancestors(pid):
            run = self._run_at(ancestor, NoLiveBuildRun())
            if isinstance(run, LiveBuildRun) or self.environment(ancestor).get("BUILDLOG_RUN"):
                return run
        return NoLiveBuildRun()


class ProcessRunEvidenceTests(unittest.TestCase):
    def test_process_run_evidence_reuses_ancestors_and_keeps_run_boundaries(self) -> None:
        for cyclic in (False, True):
            with self.subTest(cyclic=cyclic), AdmissionFixture() as fixture:
                for pid in range(1, 201):
                    parent = 3 if cyclic and pid == 1 else pid - 1
                    write_process(fixture.proc.path, pid, parent=parent)
                    directory = fixture.proc.path / str(pid)
                    _ = (directory / "cmdline").write_bytes(
                        b"bash\0verify.sh\0" if pid in (2, 17) else b"python3\0fixture.py\0")
                    environment = b"BUILDLOG_RUN=absent\0" if pid == 41 else b""
                    if pid == 88:
                        environment = b"BUILDLOG_RUN=17:17\0"
                    _ = (directory / "environ").write_bytes(environment)
                processes = fixture.proc.processes()
                activity = ReferenceActivityReader(processes, fixture.proc.path)
                expected = {pid: activity.run(pid) for pid in processes}
                reader = ScopeActivityReader(processes, fixture.proc.path)
                with mock.patch.object(reader, "command", wraps=reader.command) as command:
                    actual = {pid: reader.run(pid) for pid in processes}
                self.assertEqual(actual, expected)
                if not cyclic:
                    self.assertLessEqual(command.call_count, len(processes))
                self.assertIsInstance(actual[41], NoLiveBuildRun)
                self.assertEqual(actual[88], actual[17])


class ReplyPublicationTests(unittest.TestCase):
    def test_view_and_explain_reuse_the_bytes_of_the_completed_pass(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            server = fixture.server
            inputs, result, policy = server.last_inputs, server.last_result, server.last_policy
            assert isinstance(inputs, records.QueueInputs)
            assert isinstance(result, gate.PassResult)
            assert isinstance(policy, records.AdmissionPolicy)
            reading = replace(gate.queue_read_from_pass(inputs, policy, result), diagnostics=server.last_diagnostics)
            expected_view = wire.encode_reply(wire.ViewReply(reading)).encode()
            expected_explain = wire.encode_reply(wire.ExplainReply(reading.explanation, reading.diagnostics)).encode()
            with mock.patch.object(wire, "encode_reply", wraps=wire.encode_reply) as encode:
                _ = server.run_pass()
                encode.assert_not_called()
                view, = server.handle_request(wire.ViewRequest())
                explain, = server.handle_request(wire.ExplainRequest())
                self.assertEqual(server.reply_bytes(view), expected_view)
                self.assertEqual(server.reply_bytes(explain), expected_explain)
                self.assertEqual(encode.call_count, 2)
                encode.reset_mock()
                self.assertEqual(server.reply_bytes(view), expected_view)
                self.assertEqual(server.reply_bytes(explain), expected_explain)
                encode.assert_not_called()

    def test_pending_configuration_uses_new_values_with_the_published_reading(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            evidence = server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            reader = FixedSystemReader(evidence)
            server.system_reader = reader
            configuration = server.configuration
            assert isinstance(configuration, gate.UsableAdmissionConfiguration)
            policy = replace(configuration.config.policy, safety_bytes=9 * GIB)
            server.pending_configuration = replace(configuration, config=replace(configuration.config, policy=policy))
            reply, = server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            self.assertEqual(reply.reading.explanation.policy.safety_bytes, 9 * GIB)
            self.assertEqual(server.reply_bytes(reply), wire.encode_reply(reply).encode())
            self.assertIsInstance(server.pending_configuration, service.NoPass)

    def test_pass_uses_the_reader_evidence_without_operating_system_reads(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            server = fixture.server
            evidence = server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            reader = FixedSystemReader(replace(evidence, host=records.HostMemory(7 * GIB, 64 * GIB)))
            server.system_reader = reader
            with mock.patch.object(fixture.proc, "processes", side_effect=AssertionError("process table read")), mock.patch.object(
                    fixture.proc, "owner_start", side_effect=AssertionError("owner read")), mock.patch.object(
                    fixture.proc, "lifetime", side_effect=AssertionError("scope read")), mock.patch.object(
                    fixture.readings, "host", side_effect=AssertionError("memory read")), mock.patch.object(
                    fixture.readings, "pressure", side_effect=AssertionError("pressure read")), mock.patch.object(
                    service, "ScopeActivityReader", side_effect=AssertionError("activity read")):
                _ = server.run_pass()
            inputs = server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(inputs.host, reader.latest.host)
            self.assertTrue(reader.plans)
            used_evidence = server.system_evidence
            assert isinstance(used_evidence, service.SystemEvidence)
            self.assertEqual(used_evidence.read_at, evidence.read_at)

    def test_reader_publishes_a_complete_reading_and_wakes_the_loop(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            initial = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(initial, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            expected_plan = service.SystemReadingPlan(replace(config,
                running_pass_seconds=0.001, waiting_pass_seconds=0.001), (), (), False)
            entered = threading.Event()
            release = threading.Event()
            updated = replace(initial, read_at=initial.read_at + 1,
                              host=records.HostMemory(2 * GIB, 64 * GIB))
            threads: list[int] = []

            def read(plan: service.SystemReadingPlan, readings: service.Readings,
                     proc: service.Proc, clock: service.Clock) -> service.SystemEvidence:
                self.assertEqual(plan, expected_plan)
                self.assertIs(readings, fixture.readings)
                self.assertIs(proc, fixture.proc)
                self.assertIs(clock, fixture.clock)
                threads.append(threading.get_ident())
                entered.set()
                if not release.wait(3):
                    raise AssertionError("the test did not release the reader")
                return updated

            with mock.patch.object(service, "read_system", side_effect=read):
                reader = service.OperatingSystemReader(expected_plan, initial, fixture.readings,
                    fixture.proc, fixture.clock, server.reading_events)
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertIs(reader.latest, initial)
                    release.set()
                    self.assertTrue(select.select([server.reading_events], [], [], 3)[0])
                    self.assertIs(reader.latest, updated)
                    self.assertTrue(threads)
                    self.assertTrue(all(identity != threading.get_ident() for identity in threads))
                finally:
                    release.set()
                    reader.close()

    def test_mine_keeps_the_full_reading_and_reuses_view_bytes(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request()
            _ = fixture.server.handle_request(first)
            fixture.free(0)
            second = fixture.request()
            second = replace(second, identity=replace(second.identity, session_id="other-session"))
            _ = fixture.server.handle_request(second)
            server = fixture.server
            own, = server.handle_request(wire.MineRequest(first.identity.session_id))
            view, = server.handle_request(wire.ViewRequest())
            assert isinstance(own, wire.MineReply)
            assert isinstance(view, wire.ViewReply)
            self.assertEqual(own.reading, view.reading)
            expected = wire.encode_reply(own).encode()
            _ = server.reply_bytes(view)
            with mock.patch.object(wire, "encode_reply", side_effect=AssertionError("Mine repeated the encoding")):
                self.assertEqual(server.reply_bytes(own), expected)
                other, = server.handle_request(wire.MineRequest("other-session"))
                self.assertEqual(server.reply_bytes(other), expected)
            self.assertIn(b"other-session", expected)

    def test_reader_starts_when_an_unusable_configuration_becomes_usable(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            usable = server.configuration
            initial = server.system_evidence
            assert isinstance(initial, service.SystemEvidence)
            server.configuration = gate.UnusableAdmissionConfiguration("private invalid configuration")
            server.last_usable_configuration = service.NoPass()
            server.system_evidence = service.NoPass()
            reader = FixedSystemReader(initial)
            with mock.patch.object(service, "OperatingSystemReader", return_value=reader) as start:
                server.start_reader()
                start.assert_not_called()
                server.pending_configuration = usable
                _ = server.run_pass()
                start.assert_called_once()
                self.assertIs(server.system_reader, reader)
                self.assertTrue(reader.plans)

    def test_reader_sends_a_failure_to_the_pass_and_continues_after_an_early_read(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            initial = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(initial, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            plan = service.SystemReadingPlan(replace(config, running_pass_seconds=3600), (), (), False)
            failure = OSError("private memory read failed")
            updated = replace(initial, read_at=initial.read_at + 1)
            with mock.patch.object(service, "read_system", side_effect=[failure, updated]) as read:
                reader = service.OperatingSystemReader(plan, initial, fixture.readings,
                    fixture.proc, fixture.clock, server.reading_events)
                server.system_reader = reader
                try:
                    reader.request_read()
                    self.assertTrue(select.select([server.reading_events], [], [], 3)[0])
                    _ = os.eventfd_read(server.reading_events)
                    with self.assertRaises(OSError) as raised:
                        _ = server.run_pass()
                    self.assertIs(raised.exception, failure)
                    self.assertTrue(reader.thread.is_alive())
                    reader.request_read()
                    self.assertTrue(select.select([server.reading_events], [], [], 3)[0])
                    _ = os.eventfd_read(server.reading_events)
                    _ = server.run_pass()
                    self.assertEqual(server.system_evidence, updated)
                    self.assertEqual(read.call_count, 2)
                finally:
                    reader.close()

    def test_unknown_admit_and_launch_owners_request_one_early_read(self) -> None:
        for launch in (False, True):
            with self.subTest(launch=launch), AdmissionFixture() as fixture:
                server = fixture.server
                initial = server.system_evidence
                config = server.last_usable_configuration
                assert isinstance(initial, service.SystemEvidence)
                assert isinstance(config, records.MemoryConfig)
                reader = FixedSystemReader(initial)
                server.system_reader = reader
                request = fixture.launch() if launch else fixture.request()
                self.assertNotIn(request.owner.pid, initial.processes)
                peer = fixture.connect(request)
                self.assertEqual(peer.replies(), [])
                self.assertEqual(reader.requests, 1)
                self.assertEqual(len(server.deferred_requests), 1)
                reader.latest = service.read_system(service.SystemReadingPlan(config, (), (), False),
                    fixture.readings, fixture.proc, fixture.clock)
                os.eventfd_write(server.reading_events, 1)
                replies = peer.replies()
                self.assertTrue(replies)
                self.assertFalse(any(isinstance(reply, wire.Refused) for reply in replies))
                self.assertFalse(server.deferred_requests)
                self.assertEqual(reader.requests, 1)

    def test_new_placement_coordinator_waits_for_fresh_process_evidence(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate

        with PlacementFixture() as fixture:
            server = fixture.server
            initial = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(initial, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            reader = FixedSystemReader(initial)
            server.system_reader = reader
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            self.assertNotIn(request.owner.pid, initial.processes)
            replies = fixture.connect(request).replies()
            self.assertFalse(any(isinstance(reply, wire.Refused) for reply in replies), replies)
            self.assertEqual(len(replies), 1)
            self.assertIsInstance(replies[0], wire.PlacementWait)
            self.assertNotIn(request.episode, server.current_assignments)
            reader.latest = service.read_system(service.SystemReadingPlan(config, (), (), False),
                fixture.readings, fixture.proc, fixture.clock)
            os.eventfd_write(server.reading_events, 1)
            server.run_once(0)
            reply, = fixture.connect(request).replies()
            self.assertIsInstance(reply, wire.Assignment)

    def test_refused_deferred_connection_cannot_admit_on_a_later_read(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            initial = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(initial, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            reader = FixedSystemReader(initial)
            server.system_reader = reader
            request = fixture.request()
            peer = fixture.connect(request)
            self.assertEqual(peer.replies(), [])
            self.assertEqual(len(server.deferred_requests), 1)
            connection = next(iter(server.connections.values()))
            peer.socket.sendall(wire.encode_request(wire.ViewRequest()).encode())
            server.run_once(0)
            self.assertTrue(connection.finished)
            self.assertIn(b"one request", connection.sending)
            self.assertFalse(server.deferred_requests)
            reader.latest = service.read_system(service.SystemReadingPlan(config, (), (), False),
                fixture.readings, fixture.proc, fixture.clock)
            os.eventfd_write(server.reading_events, 1)
            server.run_once(0)
            self.assertNotIn(request.episode, server.reservations)
            self.assertNotIn(request.episode, server.ledger)
            self.assertFalse(server.deferred_requests)
            replies = peer.replies()
            self.assertEqual(len(replies), 1)
            self.assertIsInstance(replies[0], wire.Refused)

    def test_reused_pid_defers_until_the_reader_has_the_current_owner_identity(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            config = server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            request = fixture.request()
            current = service.read_system(service.SystemReadingPlan(config, (), (), False),
                fixture.readings, fixture.proc, fixture.clock)
            processes = dict(current.processes)
            process = processes[request.owner.pid]
            processes[request.owner.pid] = replace(process, start="previous-process-start")
            reader = FixedSystemReader(replace(current, processes=processes))
            server.system_reader = reader
            peer = fixture.connect(request)
            self.assertEqual(peer.replies(), [])
            self.assertEqual(reader.requests, 1)
            self.assertEqual(len(server.deferred_requests), 1)
            self.assertNotIn(request.episode, server.ledger)
            reader.latest = current
            os.eventfd_write(server.reading_events, 1)
            replies = peer.replies()
            self.assertTrue(replies)
            self.assertFalse(any(isinstance(reply, wire.Refused) for reply in replies))
            self.assertFalse(server.deferred_requests)

    def test_pending_peak_hold_restores_the_charge_from_the_same_raw_reading(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(peak_hold_seconds=1)
            request = fixture.request(need=8 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 8 * GIB)
            record = fixture.server.reservations[request.episode]
            _ = Path(record.path).with_suffix(".wave").write_text(f"{8 * GIB} {int(fixture.clock.now())}\n")
            fixture.clock.advance(10)
            server = fixture.server
            _ = server.run_pass()
            evidence = server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            server.system_reader = FixedSystemReader(evidence)
            configuration = server.configuration
            assert isinstance(configuration, gate.UsableAdmissionConfiguration)
            policy = replace(configuration.config.policy, peak_hold=records.PeakHold(1000))
            server.pending_configuration = replace(configuration, config=replace(configuration.config, policy=policy))
            reply, = server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            inputs = server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            memory, = (step.measured for step in inputs.running if gate.running_has_reservation(step))
            assert isinstance(memory, records.MeasuredMemory)
            self.assertEqual(memory.observed_peak, 8 * GIB)

    def test_width_change_uses_the_current_frozen_need_with_the_same_raw_reading(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=12 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            server = fixture.server
            old_need = records.TestPhaseNeed(GIB, 1, 8, 8, "same_width", 1.0)
            record = replace(server.reservations[request.episode], test_phase_need=old_need)
            server.reservations[request.episode] = record
            phase = gate.test_phase_path(service.reservation_row(record))
            assert isinstance(phase, Path)
            _ = phase.write_text(wire.encode_record({"test_phase_peak_bytes": GIB,
                "test_phase_started_at": fixture.clock.now(), "test_threads": 8,
                "call_id": "call-1", "test_scope": "full", "repo_identity": "fixture",
                "package": "", "filters": []}))
            _ = server.run_pass()
            evidence = server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            server.system_reader = FixedSystemReader(evidence)
            new_need = records.TestPhaseNeed(8 * GIB, 1, 16, 8, "scaled_up", 1.0)
            with mock.patch.object(gate, "test_phase_need", return_value=new_need):
                self.assertEqual(server.handle_request(wire.WidthRequest("review-width", grant.reservation, 16)), [wire.Ok()])
            self.assertEqual(server.reservations[request.episode].test_phase_need, new_need)
            _ = server.run_pass()
            inputs = server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            memory, = (step.measured for step in inputs.running if gate.running_has_reservation(step))
            assert isinstance(memory, records.MeasuredMemory)
            self.assertIsInstance(memory.peak_history, records.TestPhaseStarted)
            assert isinstance(memory.peak_history, records.TestPhaseStarted)
            self.assertEqual(memory.peak_history.need, new_need)
            fixture.free(6 * GIB)
            fixture.allow_next_start()
            following = fixture.request(need=4 * GIB)
            config = server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            assert isinstance(server.system_reader, FixedSystemReader)
            fresh = service.read_system(service.SystemReadingPlan(config, (), (), False),
                fixture.readings, fixture.proc, fixture.clock)
            server.system_reader.latest = replace(fresh, reservations=evidence.reservations)
            replies = server.handle_request(following)
            self.assertTrue(any(isinstance(reply, wire.Wait) for reply in replies))
            self.assertFalse(any(isinstance(reply, wire.Admitted) for reply in replies))

    def test_read_started_before_the_request_does_not_refuse_a_new_owner(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            evidence = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(evidence, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            reader = FixedSystemReader(evidence)
            server.system_reader = reader
            fixture.clock.advance(1)
            request = fixture.request()
            peer = fixture.connect(request)
            self.assertEqual(peer.replies(), [])
            reader.latest = replace(evidence, read_at=fixture.clock.now())
            os.eventfd_write(server.reading_events, 1)
            self.assertEqual(peer.replies(), [])
            self.assertEqual(len(server.deferred_requests), 1)
            self.assertNotIn(request.episode, server.ledger)
            fixture.clock.advance(1)
            reader.latest = service.read_system(service.SystemReadingPlan(config, (), (), False),
                fixture.readings, fixture.proc, fixture.clock)
            os.eventfd_write(server.reading_events, 1)
            replies = peer.replies()
            self.assertTrue(replies)
            self.assertFalse(any(isinstance(reply, wire.Refused) for reply in replies))

    def test_waiting_cadence_wakes_a_reader_in_its_running_delay(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            initial = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(initial, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            plan = service.SystemReadingPlan(replace(config,
                running_pass_seconds=5, waiting_pass_seconds=1), (), (), False)
            with mock.patch.object(threading.Thread, "start"):
                reader = service.OperatingSystemReader(plan, initial, fixture.readings,
                    fixture.proc, fixture.clock, server.reading_events)
            started: list[float] = []

            def delay(seconds: float) -> bool:
                self.assertEqual(seconds, 5)
                reader.update(replace(plan, waiting=True))
                fixture.clock.advance(1 if reader.requested.is_set() else seconds)
                return reader.requested.is_set()

            def read(*_args: object) -> service.SystemEvidence:
                started.append(fixture.clock.monotonic())
                reader.stopped.set()
                return initial

            with mock.patch.object(reader.requested, "wait", side_effect=delay), mock.patch.object(
                    service, "read_system", side_effect=read):
                reader.thread.run()
            self.assertEqual(started, [1])

    def test_raw_reader_does_not_apply_reservation_policy(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=8 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 8 * GIB)
            config = fixture.server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            record = fixture.server.reservations[request.episode]
            plan = service.SystemReadingPlan(config, (record,), (), False)
            with mock.patch.object(gate, "reservation_memory", side_effect=AssertionError("reservation policy")), mock.patch.object(
                    gate, "step_promise", side_effect=AssertionError("reservation promise")), mock.patch.object(
                    gate, "test_phase_history", side_effect=AssertionError("test-phase policy")):
                reading = service.read_system(plan, fixture.readings, fixture.proc, fixture.clock)
            memory = reading.reservations[request.episode].memory
            assert isinstance(memory, records.MeasuredMemory)
            self.assertEqual(memory.promise, records.NoPromise())
            self.assertEqual(memory.observed_peak, 0)
            self.assertEqual(reading.started_monotonic, fixture.clock.monotonic())
            self.assertEqual(service.reservation_charge(reading.reservations[request.episode],
                service.reservation_row(record), fixture.clock.now(), config.policy),
                gate.reservation_memory(Path(record.path), service.reservation_row(record),
                    datetime.fromtimestamp(fixture.clock.now()), fixture.proc.processes(), fixture.proc.path, config.policy))

    def test_persist_matches_full_encoding_after_each_durable_change(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server

            def assert_full_state() -> None:
                expected = service.ServiceState(server.reservations, server.places, server.ledger,
                    server.spacing, server.override, server.priority, server.build_history_generation,
                    server.run_places, server.caller_versions, server.lifecycle_outbox)
                self.assertEqual((fixture.state_dir / "state.json").read_text(), wire.encode_record(expected))

            assert_full_state()
            request = fixture.request()
            grant = only_admitted(server.handle_request(request))
            assert_full_state()
            _ = server.handle_request(wire.WidthRequest("cached-width", grant.reservation, 3))
            assert_full_state()
            record = server.reservations[request.episode]
            record.queue_waiter["session_name"] = "changed nested value"
            server.persist()
            assert_full_state()
            _ = server.handle_request(wire.TokenWaitRequest("cached-token", grant.reservation))
            assert_full_state()
            fixture.allow_next_start()
            next_request = fixture.request()
            next_grant = only_admitted(server.handle_request(next_request))
            _ = server.handle_request(wire.ReleaseRequest("cached-release", next_grant.reservation, 0))
            assert_full_state()
            _ = server.handle_request(wire.PriorityClearRequest("cached-priority", "fixture"))
            assert_full_state()

    def test_unchanged_persist_does_not_encode_durable_records_again(self) -> None:
        with AdmissionFixture() as fixture:
            _ = fixture.server.handle_request(fixture.request())
            ledger = tuple(fixture.server.ledger.values())
            encode = wire.encode_record
            seen: list[object] = []

            def observe(value: object) -> str:
                seen.append(value)
                return encode(value)

            with mock.patch.object(wire, "encode_record", side_effect=observe):
                fixture.server.persist()
            self.assertFalse(seen)
            self.assertEqual(tuple(fixture.server.ledger.values()), ledger)


class RunnerAuthorizationTests(unittest.TestCase):
    def test_refused_socket_request_keeps_saved_versions_and_state_unchanged(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.persist()
            saved = fixture.state_dir / "state.json"
            before = saved.read_bytes()
            versions = dict(fixture.server.caller_versions)
            forbidden = wire.PrioritySetRequest("runner-priority", "unit", "why", "runner",
                code_version="runner-version")
            with fixture.peer_account(os.getuid() + 10000):
                reply, = fixture.connect(forbidden).replies()
            self.assertIsInstance(reply, wire.Refused)
            self.assertEqual(fixture.server.caller_versions, versions)
            self.assertEqual(saved.read_bytes(), before)
            allowed = wire.BuildHistoryRequest(code_version="allowed-version")
            reply, = fixture.connect(allowed).replies()
            self.assertIsInstance(reply, wire.BuildHistoryReply)
            start = records.process_start_time(os.getpid())
            self.assertEqual(fixture.server.caller_versions[records.run_owner(os.getpid(), str(start))], "allowed-version")
            self.assertEqual(service.read_state(fixture.state_dir).caller_versions, fixture.server.caller_versions)

    def test_invalid_ci_label_uses_committed_configuration_and_keeps_serving(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, ci_label="CI {runner} {job}")
            commit_config_fixture(config)
            for label in ("CI {unknown}", "CI {", "CI {}", "CI {runner.missing}"):
                with self.subTest(label=label):
                    write_memory_config(config, ci_label=label)
                    resolved = records.read_memory_config(report=False)
                    self.assertEqual(resolved.ci_label, "CI {runner} {job}")
                    self.assertTrue(any("ci_label" in item.diagnostic for item in resolved.diagnostics))
                    fixture.allow_next_start()
                    request = fixture.service_request()
                    grant = only_admitted(fixture.connect(request).replies())
                    self.assertEqual(fixture.server.reservations[request.episode].queue_waiter.get("session_name", ""),
                        f"CI {request.runner} {request.job}")
                    self.assertIsInstance(fixture.connect(wire.BuildHistoryRequest()).replies()[0], wire.BuildHistoryReply)
                    fixture.release(grant)

    def test_release_completes_when_the_runner_owns_a_read_only_sample_folder(self) -> None:
        with AdmissionFixture() as fixture:
            folder = fixture.root / "runner-samples"
            folder.mkdir()
            sidecar = folder / "cargo.cgroup"
            _ = sidecar.write_text("")
            samples = (progress.progress_path(sidecar, "json"), progress.progress_path(sidecar, "log"), sidecar)
            for sample in samples[:-1]:
                _ = sample.write_text("")
            request = replace(fixture.service_request(), sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant)
            folder.chmod(0o555)
            try:
                reply, = fixture.server.handle_request(wire.ReleaseRequest("read-only-release", grant.reservation, 0))
                self.assertEqual(reply, wire.Ok())
                self.assertFalse(fixture.server.reservations)
                row, = fixture.history()
                self.assertEqual(row["status"], 0)
                self.assertTrue(all(sample.is_file() for sample in samples))
            finally:
                folder.chmod(0o755)

    def test_service_estimate_refreshes_history_and_replays_without_a_caller_run(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            fixture.configure(**{"program_first_peak_gib.cargo": 4})
            first = fixture.service_request()
            first_grant = only_admitted(fixture.server.handle_request(first))
            self.assertIsInstance(fixture.server.reservations[first.episode].run, wire.NoRun)
            fixture.allow_next_start()
            fixture.free(7 * GIB)
            second = fixture.service_request()
            waiting, = fixture.server.handle_request(second)
            self.assertIsInstance(waiting, wire.Wait)
            before = fixture.server.waiters[second.episode].row.copy()
            generation = fixture.server.build_history_generation
            fixture.scoped_peak(first_grant, 16 * GIB)
            self.assertEqual(fixture.server.handle_request(
                wire.ReleaseRequest("failed-runner", first_grant.reservation, 137)), [wire.Ok()])
            self.assertFalse(fixture.server.slots)
            self.assertGreater(fixture.server.build_history_generation, generation)
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            second_grant = only_admitted(fixture.server.handle_request(second))
            self.assertGreater(second_grant.need, first_grant.need)
            record = fixture.server.reservations[second.episode]
            self.assertEqual(record.queue_waiter["since"], before["since"])
            fixture.restart()
            self.assertEqual(only_admitted(fixture.server.handle_request(second)), second_grant)
            self.assertEqual(set(fixture.server.reservations), {second.episode})
            fixture.release(second_grant)

    def test_runner_counter_selects_the_history_window_instead_of_the_full_need(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            uid = os.getuid() + 10000
            owner = fixture.account_owner(uid)
            write_process(fixture.proc.path, owner.pid, 2 * GIB, start=owner.start_time)
            status = fixture.proc.path / str(owner.pid) / "status"
            _ = status.write_text(status.read_text() + f"Uid: {uid} {uid} {uid} {uid}\n")
            sidecar = fixture.root / "runner-counter/cargo.cgroup"
            sidecar.parent.mkdir(mode=0o755)
            _ = sidecar.write_text("")
            sidecar.chmod(0o644)
            request = replace(fixture.service_request(owner=owner), episode=f"uid:{uid}:progress", sidecar=str(sidecar))
            with fixture.peer_account(uid):
                grant = only_admitted(fixture.server.handle_request(request))
            record = fixture.server.reservations[request.episode]
            row = service.reservation_row(record)
            key = gate.reservation_profile_key(row, fixture.server.host)
            counter = progress.ProgressCounter("build:1", 40, 100)
            progress.publish_counter(sidecar, counter)
            _ = fixture.run_pass()
            without_profile, = fixture.server.handle_request(wire.ViewRequest())
            self.assertIsInstance(without_profile, wire.ViewReply)
            if isinstance(without_profile, wire.ViewReply):
                room = without_profile.reading.explanation.room
                self.assertIsInstance(room, records.MemoryRoom)
                if isinstance(room, records.MemoryRoom):
                    hold, = room.runs
                    self.assertEqual((hold.memory.need, hold.bytes), (grant.need, grant.need - 2 * GIB))
            progress.progress_path(sidecar, "json").unlink()
            for done in range(0, 100, 5):
                fixture.server.progress_history.observe("previous-ci", key,
                    progress.ProgressCounter("build:1", done, 100), 6 * GIB, fixture.clock.now())
            fixture.server.progress_history.finish("previous-ci", fixture.clock.now(), fixture.clock.now() - 86400)
            _ = fixture.run_pass()
            without_counter, = fixture.server.handle_request(wire.ViewRequest())
            self.assertIsInstance(without_counter, wire.ViewReply)
            if isinstance(without_counter, wire.ViewReply):
                room = without_counter.reading.explanation.room
                self.assertIsInstance(room, records.MemoryRoom)
                if isinstance(room, records.MemoryRoom):
                    hold, = room.runs
                    self.assertEqual((hold.memory.need, hold.bytes), (grant.need, grant.need - 2 * GIB))
            progress.publish_counter(sidecar, counter)
            _ = fixture.run_pass()
            reply, = fixture.server.handle_request(wire.ViewRequest())
            self.assertIsInstance(reply, wire.ViewReply)
            if isinstance(reply, wire.ViewReply):
                room = reply.reading.explanation.room
                self.assertIsInstance(room, records.MemoryRoom)
                if isinstance(room, records.MemoryRoom):
                    hold, = room.runs
                    self.assertEqual(hold.memory.use, records.AnonymousRunUse(2 * GIB))
                    self.assertEqual(hold.memory.need, 6 * GIB)
                    self.assertEqual(hold.bytes, 4 * GIB)
                    self.assertLess(hold.bytes, grant.need - 2 * GIB)
                measured = reply.reading.view.running[0].measured
                self.assertIsInstance(measured, records.ProgressAdjustedMemory)
                if isinstance(measured, records.ProgressAdjustedMemory):
                    self.assertEqual(measured.counter, counter)
            fixture.release(grant)

    def test_runner_cannot_change_priority_or_admit_the_head(self) -> None:
        with AdmissionFixture() as fixture, fixture.peer_account(os.getuid() + 10000):
            for request in (wire.PrioritySetRequest("set", "unit", "why", "director"),
                            wire.PriorityClearRequest("clear", "director"),
                            wire.AdmitHeadRequest("head", "why", "director")):
                with self.subTest(request=type(request).__name__):
                    replies = fixture.completed_queue_change(request)
                    self.assertEqual(len(replies), 1)
                    self.assertIsInstance(replies[0], wire.Refused)
                    self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
                    self.assertIsInstance(fixture.server.override, records.NoHeadAdmissionOverride)

    def test_runner_cannot_use_another_accounts_owner_run_or_replay(self) -> None:
        with AdmissionFixture() as fixture:
            uid = os.getuid() + 10000
            owner = fixture.account_owner(uid)
            foreign = fixture.account_owner(uid + 1)
            request = replace(fixture.request(owner=owner), episode=f"uid:{uid}:own")
            grant = only_admitted(fixture.server.handle_request(request))
            run = wire.RunIdentity(foreign.pid, foreign.start_time)
            attempts: tuple[wire.Request, ...] = (
                replace(request, episode=f"uid:{uid}:foreign-owner", owner=foreign),
                replace(request, episode=f"uid:{uid}:foreign-run", owner=fixture.account_owner(uid), run=run),
                request,
                wire.ReleaseRequest(f"uid:{uid + 1}:release", grant.reservation, 137),
                wire.WidthRequest(f"uid:{uid + 1}:width", grant.reservation, 1),
                wire.TokenWaitRequest(f"uid:{uid + 1}:token", grant.reservation),
            )
            for index, attempt in enumerate(attempts):
                peer_uid = uid if index < 2 else uid + 1
                with self.subTest(request=attempt), fixture.peer_account(peer_uid), fixture.loop_turn():
                    reply, = fixture.server.handle_request(attempt)
                    self.assertIsInstance(reply, wire.Refused)
                    self.assertEqual(set(fixture.server.reservations), {request.episode})
                    self.assertFalse(fixture.server.waiters)
            self.assertEqual(fixture.server.reservations[request.episode].test_width, request.test_width)
            fixture.release(grant)

    def test_runner_owner_identity_reads_the_private_process_start_and_account(self) -> None:
        with AdmissionFixture() as fixture:
            uid = os.getuid() + 10000
            owner = fixture.account_owner(uid)
            request = replace(fixture.request(owner=owner), episode=f"uid:{uid}:start")
            with fixture.peer_account(uid):
                granted = only_admitted(fixture.connect(request).replies())
            self.assertEqual(service.process_uid(owner.pid, fixture.proc.path), uid)
            self.assertEqual(fixture.proc.owner_start(owner.pid), service.LiveOwnerIdentity(owner.start_time))
            self.assertEqual(fixture.server.reservations[request.episode].owner, owner)
            with fixture.peer_account(uid + 1):
                refused, = fixture.connect(replace(request, owner=replace(owner, start_time="reused"))).replies()
            self.assertIsInstance(refused, wire.Refused)
            fixture.release(granted)


class WaitHolderServiceTests(unittest.TestCase):
    def test_the_wait_socket_receives_the_holder_from_the_grouped_pass(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            for need, session, owner_run in ((5, "group", run), (5, "group", run), (8, "single", wire.NO_RUN)):
                fixture.allow_next_start()
                request = fixture.request(need=need * GIB, run=owner_run)
                request = replace(request, identity=replace(request.identity, session_id=session, name=session))
                with fixture.loop_turn():
                    _ = only_admitted(fixture.server.handle_request(request))
            fixture.allow_next_start()
            fixture.free(4 * GIB)
            request = fixture.request(need=20 * GIB)
            with fixture.loop_turn():
                peer = fixture.connect(request)
                replies = peer.replies()
                waits = [reply for reply in replies if isinstance(reply, wire.Wait)]
                self.assertTrue(waits)
                self.assertEqual(waits[-1].snapshot.holder, records.SessionHolder("group", "group"))
                result = fixture.server.last_result
                assert isinstance(result, gate.PassResult)
                row = next(row for row in result.rows if row.owner == records.run_owner(
                    request.owner.pid, request.owner.start_time))
                self.assertEqual(waits[-1].snapshot.holder, row.holder)
                self.assertEqual(wire.parse_reply(wire.encode_reply(waits[-1])), waits[-1])

    def test_a_released_steps_held_slot_keeps_its_holder_identity(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            _ = (fixture.proc.path / str(run.pid) / "environ").write_bytes(
                b"CLAUDE_CODE_SESSION_ID=gap-session\0CLAUDE_SESSION_NAME=gap-worker\0")
            fixture.allow_next_start()
            request = fixture.request(need=12 * GIB, run=run)
            request = replace(request, identity=replace(request.identity, session_id="gap-session", name="gap-worker"))
            with fixture.loop_turn():
                granted = only_admitted(fixture.server.handle_request(request))
                fixture.allow_next_start()
                fixture.release(granted)
                self.assertFalse(fixture.server.reservations)
            fixture.allow_next_start()
            other = fixture.request(need=8 * GIB)
            other = replace(other, identity=replace(other.identity, session_id="other", name="other"))
            with fixture.loop_turn():
                _ = only_admitted(fixture.server.handle_request(other))
            fixture.allow_next_start()
            fixture.free(4 * GIB)
            with fixture.loop_turn():
                peer = fixture.connect(fixture.request(need=20 * GIB))
                waits = [reply for reply in peer.replies() if isinstance(reply, wire.Wait)]
                self.assertTrue(waits)
                self.assertEqual(waits[-1].snapshot.holder, records.SessionHolder("gap-session", "gap-worker"))

    def test_a_released_unit_steps_held_slot_keeps_its_unit_without_a_session(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            fixture.allow_next_start()
            request = fixture.request(need=12 * GIB, run=run)
            request = replace(request, identity=replace(request.identity, session_id="", name="", unit="gap-unit"))
            with fixture.loop_turn():
                granted = only_admitted(fixture.server.handle_request(request))
            fixture.restart()
            with fixture.loop_turn():
                fixture.allow_next_start()
                fixture.release(granted)
                self.assertFalse(fixture.server.reservations)
            fixture.allow_next_start()
            other = fixture.request(need=8 * GIB)
            with fixture.loop_turn():
                _ = only_admitted(fixture.server.handle_request(other))
            fixture.allow_next_start()
            fixture.free(4 * GIB)
            with fixture.loop_turn():
                peer = fixture.connect(fixture.request(need=20 * GIB))
                waits = [reply for reply in peer.replies() if isinstance(reply, wire.Wait)]
                self.assertTrue(waits)
                self.assertEqual(waits[-1].snapshot.holder, records.UnitHolder("gap-unit"))

    def test_a_unit_holder_reaches_the_wait_socket_without_a_session(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.allow_next_start()
            request = fixture.request(need=12 * GIB)
            request = replace(request, identity=replace(request.identity, session_id="", name="", unit="compile-unit"))
            with fixture.loop_turn():
                _ = only_admitted(fixture.server.handle_request(request))
            fixture.allow_next_start()
            fixture.free(4 * GIB)
            with fixture.loop_turn():
                peer = fixture.connect(fixture.request(need=20 * GIB))
                waits = [reply for reply in peer.replies() if isinstance(reply, wire.Wait)]
                self.assertTrue(waits)
                self.assertEqual(waits[-1].snapshot.holder, records.UnitHolder("compile-unit"))


class AdmissionCallerPersistenceTests(unittest.TestCase):

    def test_serving_rejects_direct_calls_without_loop_turn(self) -> None:
        with AdmissionFixture() as fixture, fixture.serving():
            methods: tuple[tuple[str, Callable[[], object]], ...] = (
                ("handle_request", lambda: fixture.server.handle_request(wire.ViewRequest())),
                ("persist", fixture.server.persist), ("run_pass", fixture.server.run_pass))
            for name, method in methods:
                with self.subTest(method=name):
                    with self.assertRaisesRegex(AssertionError, name + r".*use loop_turn\(\)"):
                        _ = method()
                    with fixture.loop_turn():
                        _ = method()

    def test_persistence_keeps_every_live_caller_version(self) -> None:
        with AdmissionFixture() as fixture:
            requests = [fixture.request(owner=fixture.fixture_owner(1000 + index, str(index)))
                        for index in range(64)]
            with fixture.loop_turn():
                for request in requests:
                    _ = fixture.server.handle_request(request)
                fixture.server.persist()
                expected = {f"{request.owner.pid}-{request.owner.start_time}": request.code_version
                            for request in requests}
                self.assertEqual(fixture.server.caller_versions, expected)
                self.assertEqual(service.read_state(fixture.state_dir).caller_versions, expected)


class PendingPriorityTests(unittest.TestCase):
    def test_priority_labels_use_recorded_units_without_reading_transcripts(self) -> None:
        from scripts.message import roster, unit_labels
        from scripts.production import staff

        producer = staff.StaffRow(staff.StaffId("producer"), "showrunner", "production", staff.StaffRoot(),
                                 current_session=staff.StaffId("producer-session"))
        director = staff.StaffRow(staff.StaffId("director"), "unit", "chosen", staff.ReportsTo(producer.id),
                                 current_session=staff.StaffId("director-session"))
        worker = staff.StaffRow(staff.StaffId("worker"), "worker", "builder", staff.ReportsTo(director.id),
                               current_session=staff.StaffId("worker-session"))
        snapshot = staff.StaffSnapshot((producer, director, worker))
        expected = {"director-session": UnitLabels("chosen", "production", "chosen", unit_labels.OwnerRole.UNIT),
                    "worker-session": UnitLabels("builder", "production", "chosen", unit_labels.OwnerRole.UNIT)}
        with mock.patch.object(staff, "snapshot", return_value=snapshot), \
                mock.patch.object(roster, "unit_labels_for", side_effect=AssertionError("transcript lookup")):
            self.assertEqual(gate.priority_labels(["producer-session", "director-session", "worker-session"]), expected)
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable), \
                    mock.patch.object(staff, "snapshot", side_effect=OSError("unreadable") if unreadable else None,
                                      return_value=snapshot), \
                    mock.patch.object(roster, "unit_labels_for", return_value={**expected,
                                      "unrecorded": UnitLabels("old", unit="other")}) as fallback:
                actual = gate.priority_labels(["director-session", "worker-session", "unrecorded"])
                self.assertEqual(actual, {**expected, "unrecorded": UnitLabels("old", unit="other")})
                fallback.assert_called_once_with(
                    ["director-session", "worker-session", "unrecorded"] if unreadable else ["unrecorded"], os.environ)

    def test_explicit_waiter_units_keep_order_without_a_roster_lookup(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            requests = [replace(fixture.request(), identity=wire.Identity("old", "older", "check", "", "other")),
                        replace(fixture.request(), identity=wire.Identity("new", "newer", "check", "", "chosen"))]
            for request in requests:
                _ = fixture.server.handle_request(request)
            fixture.server.priority = records.QueuePriority(({
                "unit": "chosen", "why": "urgent", "set_at": fixture.clock.now(), "set_by": "director"},))
            rows = [(fixture.state_dir / records.waiter_owner(waiting.row), waiting.row)
                    for waiting in fixture.server.waiters.values()]
            expected = gate.apply_queue_priority(rows, gate.QueueOrderContext(
                fixture.server.priority, {"old": UnitLabels("older", unit="other"),
                                          "new": UnitLabels("newer", unit="chosen")}))
            with mock.patch.object(gate, "priority_labels", side_effect=AssertionError("roster lookup")):
                _ = fixture.server.run_pass()
            assert isinstance(fixture.server.last_inputs, records.QueueInputs)
            self.assertEqual(fixture.server.last_inputs.ordered,
                             [(path, records.stored_waiter(row)) for path, row in expected[0]])

    def test_runner_retry_cannot_attach_to_a_pending_priority_request(self) -> None:
        with AdmissionFixture() as fixture, self.delayed_roster() as (entered, finish), fixture.serving():
            try:
                request = wire.PrioritySetRequest("pending-owner", "chosen", "urgent", "director")
                first = self.connect(fixture, request)
                self.assertTrue(entered.wait(timeout=3))
                with fixture.peer_account(os.getuid() + 10000):
                    reply, = fixture.completed_queue_change(request)
                self.assertIsInstance(reply, wire.Refused)
                with fixture.loop_turn():
                    pending = fixture.server.pending_priority
                    self.assertIsInstance(pending, service.PendingPriorityRoster)
                    if isinstance(pending, service.PendingPriorityRoster):
                        self.assertEqual(len(pending.change.connections), 1)
                finish.set()
                self.assertEqual(self.reply(first), wire.Ok())
                self.assertEqual(fixture.completed_queue_change(request), [wire.Ok()])
            finally:
                finish.set()

    def connect(self, fixture: AdmissionFixture, request: wire.Request) -> socket.socket:
        peer = self.enterContext(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        peer.connect(str(fixture.socket_path))
        peer.sendall(wire.encode_request(request).encode())
        return peer

    def reply(self, peer: socket.socket) -> wire.Reply:
        stream = self.enterContext(peer.makefile("rb"))
        return wire.parse_reply(stream.readline())

    @contextmanager
    def delayed_roster(self, session_id: str = "worker") -> Generator[tuple[threading.Event, threading.Event]]:
        entered, finish = threading.Event(), threading.Event()

        def read_roster(sessions: list[str] | gate.RosterUnits) -> dict[str, UnitLabels]:
            if isinstance(sessions, gate.RosterUnits):
                entered.set()
                _ = finish.wait()
            return {session_id: UnitLabels("worker", unit="chosen")}

        with mock.patch.object(gate, "priority_labels", side_effect=read_roster):
            try:
                yield entered, finish
            finally:
                finish.set()

    def test_query_and_release_reply_while_a_priority_roster_read_is_pending(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            with self.delayed_roster() as (entered, finish), fixture.serving():
                try:
                    priority = self.connect(fixture, wire.PrioritySetRequest("set", "chosen", "urgent", "director"))
                    self.assertTrue(entered.wait())
                    viewed = self.reply(self.connect(fixture, wire.ViewRequest()))
                    self.assertIsInstance(viewed, wire.ViewReply)
                    self.assertEqual(self.reply(self.connect(fixture,
                        wire.ReleaseRequest("release", grant.reservation, 0))), wire.Ok())
                    with fixture.loop_turn():
                        self.assertNotIn(request.episode, fixture.server.reservations)
                        self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
                    self.assertEqual(select.select([priority], [], [], 0)[0], [])
                    finish.set()
                    self.assertEqual(self.reply(priority), wire.Ok())
                    viewed = self.reply(self.connect(fixture, wire.ViewRequest()))
                    assert isinstance(viewed, wire.ViewReply)
                    applied = viewed.reading.explanation.priority
                    assert not isinstance(applied, records.NoQueuePriority)
                    self.assertEqual((applied.entries[-1]["unit"], applied.entries[-1]["why"], applied.entries[-1]["set_by"]),
                                     ("chosen", "urgent", "director"))
                finally:
                    finish.set()

    def test_clear_waits_for_a_pending_set_and_leaves_no_priority(self) -> None:
        with AdmissionFixture() as fixture, self.delayed_roster() as (entered, finish), fixture.serving():
            try:
                priority = self.connect(fixture, wire.PrioritySetRequest("set", "chosen", "urgent", "director"))
                self.assertTrue(entered.wait())
                cleared = self.connect(fixture, wire.PriorityClearRequest("clear", "director"))
                self.assertIsInstance(self.reply(self.connect(fixture, wire.ViewRequest())), wire.ViewReply)
                self.assertEqual(select.select([cleared], [], [], 0)[0], [])
                finish.set()
                self.assertEqual(self.reply(priority), wire.Ok())
                self.assertEqual(self.reply(cleared), wire.Ok())
                viewed = self.reply(self.connect(fixture, wire.ViewRequest()))
                assert isinstance(viewed, wire.ViewReply)
                self.assertIsInstance(viewed.reading.explanation.priority, records.NoQueuePriority)
                with fixture.loop_turn():
                    self.assertIsInstance(service.read_state(fixture.state_dir).priority, records.NoQueuePriority)
            finally:
                finish.set()

    def test_pending_priority_retry_gets_one_reply_and_records_one_transition(self) -> None:
        with AdmissionFixture() as fixture, self.delayed_roster() as (entered, finish), \
             mock.patch.object(buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged, \
             fixture.serving():
            try:
                request = wire.PrioritySetRequest("same-key", "chosen", "urgent", "director")
                first = self.connect(fixture, request)
                self.assertTrue(entered.wait())
                retry = self.connect(fixture, request)
                self.assertIsInstance(self.reply(self.connect(fixture, wire.ViewRequest())), wire.ViewReply)
                self.assertEqual(select.select([first, retry], [], [], 0)[0], [])
                finish.set()
                for peer in (first, retry):
                    self.assertEqual(self.reply(peer), wire.Ok())
                    self.assertEqual(peer.recv(1), b"")
                self.assertEqual(self.reply(self.connect(fixture, request)), wire.Ok())
                self.assertEqual(logged.call_count, 1)
                self.assertEqual(logged.call_args.kwargs["transition_id"], request.request_id)
            finally:
                finish.set()

    def test_head_admission_waits_for_the_pending_priority_set(self) -> None:
        with AdmissionFixture() as fixture:
            _ = fixture.occupy_host()
            fixture.free(16 * GIB)
            fixture.hold_starts()
            older = fixture.request(need=12 * GIB, since=fixture.clock.now() - 60)
            request = fixture.request(need=12 * GIB)
            _ = fixture.server.handle_request(older)
            _ = fixture.server.handle_request(request)
            with self.delayed_roster(request.identity.session_id) as (entered, finish), fixture.serving():
                try:
                    priority = self.connect(fixture, wire.PrioritySetRequest("set", "chosen", "urgent", "director"))
                    self.assertTrue(entered.wait())
                    admitted = self.connect(fixture, wire.AdmitHeadRequest("head", "start", "director"))
                    self.assertIsInstance(self.reply(self.connect(fixture, wire.ViewRequest())), wire.ViewReply)
                    self.assertEqual(select.select([admitted], [], [], 0)[0], [])
                    with fixture.loop_turn():
                        self.assertIsInstance(fixture.server.override, records.NoHeadAdmissionOverride)
                    finish.set()
                    self.assertEqual(self.reply(priority), wire.Ok())
                    self.assertEqual(self.reply(admitted), wire.Ok())
                    with fixture.loop_turn():
                        override = fixture.server.override
                        assert not isinstance(override, records.NoHeadAdmissionOverride)
                        self.assertEqual(override["pid"], request.owner.pid)
                finally:
                    finish.set()

    def test_restart_during_a_roster_read_keeps_priority_and_allows_the_same_key_retry(self) -> None:
        with AdmissionFixture() as fixture:
            labels = {"worker": UnitLabels("worker", unit="original")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                    "initial", "original", "keep", "director")), [wire.Ok()])
            with self.delayed_roster() as (entered, finish), fixture.serving():
                try:
                    request = wire.PrioritySetRequest("retry-after-restart", "chosen", "urgent", "director")
                    pending = self.connect(fixture, request)
                    self.assertTrue(entered.wait())
                    with fixture.loop_turn():
                        saved = service.read_state(fixture.state_dir)
                        assert not isinstance(saved.priority, records.NoQueuePriority)
                        self.assertEqual(saved.priority.entries[0]["unit"], "original")
                        self.assertNotIn(request.request_id, saved.ledger)
                        fixture.restart()
                        restored = fixture.server.priority
                        assert not isinstance(restored, records.NoQueuePriority)
                        self.assertEqual(restored.entries[0]["unit"], "original")
                    self.assertEqual(pending.recv(1), b"")
                    finish.set()
                    self.assertEqual(self.reply(self.connect(fixture, request)), wire.Ok())
                    viewed = self.reply(self.connect(fixture, wire.ViewRequest()))
                    assert isinstance(viewed, wire.ViewReply)
                    applied = viewed.reading.explanation.priority
                    assert not isinstance(applied, records.NoQueuePriority)
                    self.assertEqual(applied.entries[-1]["unit"], "chosen")
                finally:
                    finish.set()

    def test_roster_error_refuses_without_replay_and_allows_a_same_key_retry(self) -> None:
        with AdmissionFixture() as fixture:
            request = wire.PrioritySetRequest("roster-retry", "chosen", "urgent", "director")

            def unreadable_roster(sessions: list[str] | gate.RosterUnits) -> dict[str, UnitLabels]:
                if isinstance(sessions, gate.RosterUnits):
                    raise ValueError("roster unreadable")
                return {}

            with mock.patch.object(gate, "priority_labels", side_effect=unreadable_roster):
                self.assertEqual(fixture.completed_queue_change(request), [wire.Refused("roster unreadable")])
            self.assertIsInstance(fixture.completed_queue_change(wire.ViewRequest())[0], wire.ViewReply)
            self.assertNotIn(request.request_id, service.read_state(fixture.state_dir).ledger)
            with mock.patch.object(gate, "priority_labels", return_value={
                    "worker": UnitLabels("worker", unit="chosen")}):
                self.assertEqual(fixture.completed_queue_change(request), [wire.Ok()])
            priority = fixture.server.priority
            assert not isinstance(priority, records.NoQueuePriority)
            self.assertEqual(priority.entries[-1]["unit"], "chosen")

    def test_apply_errors_refuse_pending_and_queued_changes_and_allow_later_requests(self) -> None:
        with AdmissionFixture() as fixture, self.delayed_roster() as (entered, finish), fixture.serving():
            try:
                request = wire.PrioritySetRequest("apply-set", "chosen", "urgent", "director")
                clear = wire.PriorityClearRequest("apply-clear", "director")
                priority = self.connect(fixture, request)
                self.assertTrue(entered.wait())
                cleared = self.connect(fixture, clear)
                following = self.connect(fixture, wire.AdmitHeadRequest("following", "start", "director"))
                self.assertIsInstance(self.reply(self.connect(fixture, wire.ViewRequest())), wire.ViewReply)
                self.assertEqual(select.select([priority, cleared, following], [], [], 0)[0], [])
                with mock.patch.object(store, "read_last_whole_row_end", side_effect=ValueError("history unreadable")):
                    finish.set()
                    self.assertEqual(self.reply(priority), wire.Refused("history unreadable"))
                    self.assertEqual(self.reply(cleared), wire.Refused("history unreadable"))
                    self.assertEqual(self.reply(following), wire.Refused("nothing waits; no head to admit"))
                self.assertIsInstance(self.reply(self.connect(fixture, wire.ViewRequest())), wire.ViewReply)
                with fixture.loop_turn():
                    saved = service.read_state(fixture.state_dir)
                    self.assertNotIn(request.request_id, saved.ledger)
                    self.assertNotIn(clear.request_id, saved.ledger)
                self.assertEqual(self.reply(self.connect(fixture, request)), wire.Ok())
                self.assertEqual(self.reply(self.connect(fixture, clear)), wire.Ok())
                viewed = self.reply(self.connect(fixture, wire.ViewRequest()))
                assert isinstance(viewed, wire.ViewReply)
                self.assertIsInstance(viewed.reading.explanation.priority, records.NoQueuePriority)
            finally:
                finish.set()


class OutageClaimTests(unittest.TestCase):
    def test_outage_claim_clears_once_after_listener_uptime_using_the_last_usable_config(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = time.time()
            fixture.configure(silence_seconds=7)
            claimed = outage_alerts.claim("build-admission")
            assert isinstance(claimed, outage_alerts.AlertClaim)
            outage_alerts.delivered(claimed)
            fixture.clock.advance(100)
            with fixture.loop_turn():
                fixture.restart()
                fixture.server.run_once()
                fixture.clock.advance(6)
                fixture.server.run_once()
                self.assertIsInstance(outage_alerts.claim("build-admission"), outage_alerts.AlertDelivered)
                fixture.configure(silence_seconds=float("nan"))
                fixture.server.run_once()
                self.assertIsInstance(fixture.server.configuration, gate.UnusableAdmissionConfiguration)
                fixture.clock.advance(1)
                with mock.patch.object(outage_alerts, "clear", wraps=outage_alerts.clear) as clear:
                    fixture.server.run_once()
                    fixture.clock.advance(7)
                    fixture.server.run_once()
                self.assertEqual([call for call in clear.call_args_list if call.args[0] == "build-admission"],
                    [mock.call("build-admission", fixture.clock.now() - 7)])
            next_claim = outage_alerts.claim("build-admission")
            self.assertIsInstance(next_claim, outage_alerts.AlertClaim)

    def test_failed_outage_claim_clear_reports_once_per_attempt_and_retries_in_the_loop(self) -> None:
        for failure in (False, OSError("ledger unavailable")):
            with self.subTest(failure=failure), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=7)
                with fixture.loop_turn():
                    fixture.restart()
                    fixture.server.run_once()
                    fixture.clock.advance(7)
                    with mock.patch.object(outage_alerts, "clear", side_effect=[failure, True]) as clear, \
                            redirect_stderr(io.StringIO()) as errors:
                        fixture.server.run_once()
                        self.assertEqual(clear.call_args_list, [mock.call("build-admission", fixture.clock.now())])
                        self.assertEqual(len(errors.getvalue().splitlines()), 1)
                        self.assertIn("cannot clear outage alert", errors.getvalue())
                        fixture.server.run_once()
                        fixture.server.run_once()
                        self.assertEqual(clear.call_args_list,
                            [mock.call("build-admission", fixture.clock.now())] * 2)
                        self.assertEqual(len(errors.getvalue().splitlines()), 1)


class ProgressProfileServiceTests(unittest.TestCase):
    def test_configured_rank_sets_the_remaining_memory_need(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            write_memory_config(fixture.root / "build_admission.conf", progress_profile_rank="2")
            owner = fixture.proc.spawn(used=GIB)
            sidecar = fixture.scope(owner, current=GIB, anonymous=GIB)
            request = replace(fixture.request(owner=owner, step="build", need=10 * GIB), sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            row = service.reservation_row(fixture.server.reservations[request.episode])
            key = gate.reservation_profile_key(row, fixture.server.host)
            for index, need in enumerate((9 * GIB, 3 * GIB, 6 * GIB)):
                for done in range(0, 100, 5):
                    fixture.server.progress_history.observe(f"previous-{index}", key,
                        progress.ProgressCounter("build:1", done, 100), need, fixture.clock.now())
                fixture.server.progress_history.finish(f"previous-{index}", fixture.clock.now(), fixture.clock.now() - 1)
            progress.publish_counter(sidecar, progress.ProgressCounter("build:1", 50, 100))
            checked = gate.read_admission_configuration()
            self.assertIsInstance(checked, gate.UsableAdmissionConfiguration)
            write_memory_config(fixture.root / "build_admission.conf", peak_history_share_percent="70")
            with mock.patch.object(gate, "read_admission_configuration", return_value=checked):
                _ = fixture.run_pass()
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            room = reply.reading.explanation.room
            assert isinstance(room, records.MemoryRoom)
            hold, = room.runs
            self.assertEqual(hold.memory.need, 6 * GIB)
            self.assertEqual(hold.bytes, 5 * GIB)
            fixture.release(grant)

    def test_saved_highest_bin_names_the_scope_processes_and_reading_source(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            owner = fixture.proc.spawn(used=GIB // 4)
            helper = fixture.proc.spawn(used=11 * GIB // 4)
            helper_proc = fixture.proc.path / str(helper.pid)
            _ = (helper_proc / "cmdline").write_bytes(b"rustc-outlier\0--private-fixture\0")
            _ = (helper_proc / "comm").write_text("rustc-outlier\n")
            sidecar = fixture.scope(owner, members=(owner.pid, helper.pid), current=4 * GIB, anonymous=3 * GIB)
            scope = Path(sidecar.read_text().strip())
            request = replace(fixture.request(owner=owner, step="merge-gate", need=4 * GIB), sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            progress.publish_counter(sidecar, progress.ProgressCounter("suites:1", 20, 100))
            _ = fixture.run_pass()
            _ = (scope / "cgroup.procs").write_text(f"{owner.pid}\n")
            _ = (scope / "memory.stat").write_text(f"anon {GIB}\nshmem 0\ninactive_file 0\n")
            progress.publish_counter(sidecar, progress.ProgressCounter("suites:1", 80, 100))
            _ = fixture.run_pass()
            fixture.release(grant)
            path, = fixture.server.progress_history.root.glob("*/*.json")
            record = progress.read_profile_record(path)
            assert isinstance(record, progress.RecordedProfile)
            self.assertEqual(record.profile.phases[0].bins[4], 3 * GIB)
            self.assertEqual(record.profile.phases[0].bins[16], GIB)
            self.assertEqual(record.reading_source, progress.ScopeReading(scope))
            peak = record.peak_processes
            assert isinstance(peak, progress.PeakProcesses)
            self.assertEqual((peak.phase, peak.bin, peak.anonymous_bytes), ("suites:1", 4, 3 * GIB))
            self.assertEqual({process.pid for process in peak.processes}, {owner.pid, helper.pid})
            helper_sample, = (process for process in peak.processes if process.pid == helper.pid)
            self.assertEqual(helper_sample.command, ("rustc-outlier", "--private-fixture"))
            self.assertEqual(helper_sample.rss_anon, 11 * GIB // 4)

    def test_a_version_four_snapshot_is_rejected_until_a_new_pass_publishes_current_evidence(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            _ = fixture.run_pass()
            path = fixture.state_dir / "queue.json"
            envelope = cast(dict[str, object], json.loads(path.read_text()))
            envelope["v"] = 4
            _ = path.write_text(json.dumps(envelope))
            self.assertIsInstance(queue_snapshot.read_queue_snapshot(fixture.state_dir),
                queue_snapshot.NoQueueSnapshot)
            _ = fixture.run_pass()
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.inputs, fixture.server.last_inputs)

    def test_shared_scope_writes_one_anonymous_sample_across_distinct_step_keys(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            owners = (fixture.proc.spawn(), fixture.proc.spawn())
            run = fixture.run_owner()
            sidecars = tuple(fixture.scope(owner, folder="shared-progress", members=tuple(item.pid for item in owners),
                current=20 * GIB, anonymous=3 * GIB, swap=9 * GIB) for owner in owners)
            grants: list[wire.Admitted] = []
            for step, owner, sidecar in zip(("build", "clippy"), owners, sidecars, strict=True):
                request = replace(fixture.request(owner=owner, step=step, episode=step,
                    need=4 * GIB, run=run), sidecar=str(sidecar))
                grants.append(only_admitted(fixture.server.handle_request(request)))
                fixture.allow_next_start()
            for sidecar in sidecars:
                progress.publish_counter(sidecar, progress.ProgressCounter("build:1", 80, 100))
            _ = fixture.run_pass()
            rows = [service.reservation_row(record) for record in fixture.server.reservations.values()]
            for grant in grants:
                fixture.release(grant)
            samples: list[int] = []
            for row in rows:
                profile = fixture.server.progress_history.matching(
                    gate.reservation_profile_key(row, store.host_name()), fixture.clock.now() - 1)
                peak = progress.window_peak(profile, progress.ProgressCounter("build:1", 80, 100), 1)
                if isinstance(peak, progress.WindowPeak):
                    samples.append(peak.bytes)
            self.assertEqual(samples, [3 * GIB])

    def test_unavailable_anonymous_use_keeps_the_full_need_and_writes_no_profile(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            owner = fixture.proc.spawn()
            sidecar = fixture.scope(owner, current=20 * GIB, anonymous=3 * GIB, swap=9 * GIB)
            request = replace(fixture.request(owner=owner, step="build", need=4 * GIB),
                sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            progress.publish_counter(sidecar, progress.ProgressCounter("build:1", 80, 100))
            _ = (Path(sidecar.read_text().strip()) / "memory.stat").write_text("inactive_file 0\n")
            _ = fixture.run_pass()
            record, = fixture.server.reservations.values()
            row = service.reservation_row(record)
            inputs, policy = fixture.server.last_inputs, fixture.server.last_policy
            assert isinstance(inputs, records.QueueInputs)
            assert isinstance(policy, records.AdmissionPolicy)
            room = gate.inputs_room(inputs, policy)
            assert isinstance(room, records.MemoryRoom)
            self.assertEqual(room.runs[0].memory.need, 4 * GIB)
            fixture.release(grant)
            profile = fixture.server.progress_history.matching(
                gate.reservation_profile_key(row, store.host_name()), fixture.clock.now() - 1)
            self.assertIsInstance(profile, progress.NoMatchingProfile)


class ReadingAlertTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        sender = Path(self.enterContext(tempfile.TemporaryDirectory())) / "message/send.py"
        sender.parent.mkdir()
        _ = sender.write_text("raise AssertionError('unexpected fixture sender execution')\n")
        _ = self.enterContext(mock.patch.object(session_send, "SEND_SCRIPT", sender))

    @staticmethod
    def finish_delivery(fixture: AdmissionFixture) -> None:
        delivery = fixture.server.reading_alert_delivery
        if isinstance(delivery, service.ReadingAlertDelivery):
            delivery.thread.join(timeout=3)
            if delivery.thread.is_alive():
                raise AssertionError("the stub sender did not finish")

    def test_one_message_per_unreadable_stretch_and_another_after_recovery(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = time.time()
            fixture.configure(reading_alert_seconds=10)
            request = fixture.request()
            path = fixture.root / "pressure"
            path.unlink()
            messages: list[tuple[list[str], str]] = []

            def send(command: list[str], **kwargs: str | bool | float) -> subprocess.CompletedProcess[str]:
                if any(part.endswith("message/send.py") for part in command):
                    messages.append((command, str(kwargs["input"])))
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"unexpected subprocess during alert delivery: {command}")

            with mock.patch("scripts.build_queue.admission_service.subprocess.run", side_effect=send):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
                fixture.clock.advance(9)
                _ = fixture.run_pass()
                self.assertEqual(messages, [])
                fixture.clock.advance(1)
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(messages), 1)
                command, message = messages[0]
                self.assertEqual(command[command.index("--to") + 1], "natedev")
                self.assertEqual(command[command.index("--from") + 1], "build-admission")
                self.assertIn(str(path), message)
                self.assertIn("10 seconds", message)
                fixture.clock.advance(10)
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(messages), 1)
                write_pressure_reading(path)
                _ = fixture.run_pass()
                self.assertIsInstance(fixture.server.reading_hold, service.NoReadingHold)
                path.unlink()
                self.assertIsInstance(fixture.server.handle_request(fixture.request())[0], wire.Wait)
                fixture.clock.advance(10)
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(messages), 2)

    def test_failed_delivery_releases_the_claim_for_retry(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(reading_alert_seconds=1)
            request = fixture.request()
            (fixture.root / "pressure").unlink()
            _ = fixture.server.handle_request(request)
            fixture.clock.advance(1)
            attempts: list[str] = []

            def send(command: list[str], **kwargs: str | bool | float) -> subprocess.CompletedProcess[str]:
                if any(part.endswith("message/send.py") for part in command):
                    attempts.append(str(kwargs["input"]))
                    return subprocess.CompletedProcess(command, 2 if len(attempts) == 1 else 0, "", "stub failure")
                raise AssertionError(f"unexpected subprocess during alert delivery: {command}")

            with mock.patch("scripts.build_queue.admission_service.subprocess.run", side_effect=send):
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(attempts), 1)
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(attempts), 2)
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(attempts), 2)

    def test_admission_pass_does_not_wait_for_the_sender(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(reading_alert_seconds=1)
            request = fixture.request()
            (fixture.root / "pressure").unlink()
            _ = fixture.server.handle_request(request)
            fixture.clock.advance(1)
            entered = threading.Event()
            release = threading.Event()

            def send(command: list[str], **kwargs: str | bool | float) -> subprocess.CompletedProcess[str]:
                if any(part.endswith("message/send.py") for part in command):
                    self.assertIn("unreadable", str(kwargs["input"]))
                    entered.set()
                    if not release.wait(timeout=3):
                        raise AssertionError("the admission pass waited for the sender")
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"unexpected subprocess during alert delivery: {command}")

            with mock.patch("scripts.build_queue.admission_service.subprocess.run", side_effect=send):
                try:
                    result = fixture.run_pass()
                    self.assertTrue(entered.wait(timeout=2))
                    self.assertEqual(result.rows[0].decision.state, "hold")
                    delivery = fixture.server.reading_alert_delivery
                    assert isinstance(delivery, service.ReadingAlertDelivery)
                    self.assertTrue(delivery.thread.is_alive())
                    _ = fixture.run_pass()
                    self.assertIs(fixture.server.reading_alert_delivery, delivery)
                finally:
                    release.set()
                    self.finish_delivery(fixture)

    def test_no_timer_for_an_empty_queue_a_token_wait_or_a_slot_continuation(self) -> None:
        for state in ("empty", "token", "slot"):
            with self.subTest(state=state), AdmissionFixture() as fixture:
                fixture.configure(reading_alert_seconds=1)
                run = fixture.run_owner()
                request = fixture.request(run=run)
                if state != "empty":
                    grant = only_admitted(fixture.server.handle_request(request))
                    if state == "token":
                        self.assertEqual(fixture.server.handle_request(wire.TokenWaitRequest("token", grant.reservation)), [wire.Ok()])
                    else:
                        self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0)), [wire.Ok()])
                _ = fixture.enter_context(mock.patch("scripts.build_queue.admission_service.subprocess.run",
                    return_value=subprocess.CompletedProcess(["stub"], 0, "", "")))
                (fixture.root / "pressure").unlink()
                if state == "slot":
                    held, = fixture.server.handle_request(fixture.request(run=run, age=600))
                    assert isinstance(held, wire.Wait)
                    assert isinstance(held.reason, records.HeadAdmissionStatus)
                    self.assertIsInstance(held.reason.decision.room.basis, records.WithinSlot)
                    self.assertIsInstance(held.reason.cause, records.EstimateExpired)
                _ = fixture.run_pass()
                fixture.clock.advance(2)
                _ = fixture.run_pass()
                self.assertIsInstance(fixture.server.reading_hold, service.NoReadingHold)
                self.assertIsInstance(fixture.server.reading_alert_delivery, service.NoReadingAlertDelivery)


class PlacementRestartTests(unittest.TestCase):
    def test_first_view_after_restart_uses_the_snapshot_while_a_probe_succeeds_slowly(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import PlacementFixture, placement_request

        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            fixture.server.placement_requests = {
                f"saved-{index}": replace(request, episode=f"saved-{index}",
                    owner=request.owner if index == 0 else placement.SubmittingOwner(
                        fixture.server.host, 700000 + index, "missing"))
                for index in range(30)}
            fixture.server.persist()
            fixture.server.close()
            entered, succeeded = threading.Event(), threading.Event()
            release = threading.Event()

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the probe")
                succeeded.set()
                return placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())

            with mock.patch.object(fixture, "read_provider", side_effect=probe):
                fixture.server = fixture.new_server()
                try:
                    self.assertEqual(len(fixture.server.placement_requests), 30)
                    self.assertTrue(entered.wait(1))
                    with fixture.serving():
                        reading = client.view()
                        self.assertIsInstance(reading, records.QueueRead)
                        self.assertFalse(succeeded.is_set())
                        release.set()
                        self.assertTrue(succeeded.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                    with mock.patch.object(fixture.server, "host_evidence_key",
                                           side_effect=AssertionError("a view must use cached evidence")):
                        reply, = fixture.server.handle_request(wire.ViewRequest())
                        self.assertIsInstance(reply, wire.ViewReply)
                finally:
                    for worker in fixture.server.host_targets.values():
                        worker.close()
                    release.set()
                    for worker in fixture.server.host_targets.values():
                        worker.thread.join(timeout=5)
                        self.assertFalse(worker.thread.is_alive())

    def test_restart_preserves_old_waiters_and_both_placement_permission_states(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
        )

        for version in (1, 2):
            with self.subTest(version=version), PlacementFixture() as fixture:
                fixture.configure(cloud_gate_slots=1)
                fixture.free(0)
                blocker = replace(fixture.request(), place=wire.CloudPlace("aws"))
                _ = only_admitted(fixture.server.handle_request(blocker))
                fixture.hold_starts()
                local = fixture.request(since=fixture.clock.now() - 100)
                cloud = replace(fixture.request(since=fixture.clock.now() - 50), place=wire.CloudPlace("aws"))
                legacy_messages: list[str] = []
                for request in (local, cloud):
                    envelope = cast(dict[str, object], json.loads(wire.encode_request(request)))
                    envelope["v"] = version
                    message = json.dumps(envelope) + "\n"
                    legacy_messages.append(message)
                    reply, = fixture.connect(message).replies()
                    self.assertIsInstance(reply, wire.Wait)
                pending = placement_request(fixture, since=fixture.clock.now() - 25)
                consumed = placement_request(fixture, since=fixture.clock.now() - 10)
                _ = publish_candidate(fixture, pending)
                pending_assignment = assignment_for(fixture, pending)
                consumed_assignment = assignment_for(fixture, consumed)
                claim = adopt_assignment(fixture, consumed, consumed_assignment)
                consume = consume_permission("first-consumption", consumed_assignment, claim)
                self.assertEqual(fixture.server.handle_request(consume),
                                 [wire.FirstAuthorization(consumed_assignment, claim)])
                positions = fixture.server.places.copy()
                waiters = {episode: fixture.server.waiters[episode].request for episode in (local.episode, cloud.episode)}
                fixture.restart()
                self.assertEqual(fixture.server.places, positions)
                for message in legacy_messages:
                    reply, = fixture.connect(message).replies()
                    self.assertIsInstance(reply, wire.Wait)
                self.assertEqual({episode: fixture.server.waiters[episode].request for episode in waiters}, waiters)
                self.assertEqual(assignment_for(fixture, pending), pending_assignment)
                consumed_reply, = fixture.connect(consumed).replies()
                self.assertIsInstance(consumed_reply, wire.PlacementWait)
                saved = fixture.server.assignments[placement.attempt_key(
                    consumed_assignment.episode, consumed_assignment.attempt)]
                self.assertEqual(saved.assignment, consumed_assignment)
                self.assertIsInstance(saved.state, placement.CurrentAssignment)
                self.assertIsInstance(saved.permission, placement.PermissionConsumed)
                status, = fixture.server.handle_request(consume)
                self.assertIsInstance(status, wire.DispatchStatus)
                assert isinstance(status, wire.DispatchStatus)
                self.assertIsInstance(status.permission, placement.PermissionConsumed)
                pending_claim = adopt_assignment(fixture, pending, pending_assignment)
                self.assertEqual(fixture.server.handle_request(consume_permission(
                    "pending-consumption", pending_assignment, pending_claim)),
                    [wire.FirstAuthorization(pending_assignment, pending_claim)])


class CloudAdmissionTests(unittest.TestCase):
    def test_cloud_and_memory_settings_share_the_checked_pass_and_failure_path(self) -> None:
        from scripts.build_queue import cloud_progress

        for key in ("cloud_gate_slots", "cloud_gate_stale_pushes", "safety_gib", "peak_history_share_percent"):
            with self.subTest(key=key), AdmissionFixture() as fixture:
                config_path = fixture.root / "build_admission.conf"
                write_memory_config(config_path, cloud_gate_slots="1", cloud_gate_stale_pushes="3")
                sidecar = fixture.root / "cloud.cgroup"
                _ = sidecar.write_text("")
                request = replace(self.cloud_request(fixture), sidecar=str(sidecar))
                grant = only_admitted(fixture.server.handle_request(request))
                cloud_progress.publish(sidecar, cloud_progress.CloudProgress("running", 32, 0, fixture.clock.now() - 15))
                waiting = self.cloud_request(fixture)
                self.assertIsInstance(fixture.server.handle_request(waiting)[0], wire.Wait)
                local_requests: list[wire.AdmitRequest] = []
                if key == "peak_history_share_percent":
                    local = fixture.request()
                    assert isinstance(local.profile, records.StepProfile)
                    local = replace(local, profile=replace(local.profile,
                        repository=records.RepositoryIdentity("fixture-repository", "fixture")))
                    store.append_line(fixture.state_dir / "anon_peaks.jsonl", {
                        "host": fixture.server.host, "repo_identity": "fixture-repository", "step": "nextest",
                        "test_threads": 8, "test_phase_peak_bytes": 3 * GIB,
                        "ended_at": store.utc_iso(fixture.clock.now() - 1)})
                    fixture.hold_starts()
                    self.assertIsInstance(fixture.server.handle_request(local)[0], wire.Wait)
                    local_requests.append(local)
                    write_pressure_reading(fixture.root / "pressure")
                    fixture.allow_next_start()
                read_configuration = gate.read_admission_configuration

                def change_after_check() -> gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration:
                    configuration = read_configuration()
                    write_memory_config(config_path, **{key: "invalid"})
                    return configuration

                with mock.patch.object(gate, "read_admission_configuration", side_effect=change_after_check):
                    fixture.server.next_pass = fixture.clock.monotonic()
                    fixture.server.run_once(0)
                self.assertIsInstance(fixture.server.configuration, gate.UsableAdmissionConfiguration)
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply) and isinstance(reply.reading, wire.CloudQueueRead)
                self.assertEqual(reply.reading.cloud_slots, 1)
                self.assertEqual(reply.reading.cloud_gate_stale_pushes, 3)
                self.assertIn(waiting.episode, fixture.server.waiters)
                for local in local_requests:
                    self.assertEqual(fixture.server.reservations[local.episode].test_phase_need,
                                     records.TestPhaseNeed(3 * GIB, 1, 8, 8, "same_width", margin=1.2))
                model = queue_view.QueueModel(reply.reading, {}, records.NoWaiter(), head_facts.PaceUnavailable(),
                    ZoneInfo("UTC"), queue_view.HostLoadUnavailable(), records.NoWaiter())
                with mock.patch.object(queue_view, "cloud_push_interval", return_value=queue_view.CloudPushInterval(5)):
                    text = plain(queue_view.render_view(model, 140))
                self.assertIn("no update for", text)
                fixture.server.next_pass = fixture.clock.monotonic()
                fixture.server.run_once(0)
                self.assertIsInstance(fixture.server.configuration, gate.UnusableAdmissionConfiguration)
                refused, = fixture.connect(wire.ViewRequest()).replies()
                self.assertIsInstance(refused, wire.Refused)
                assert isinstance(refused, wire.Refused)
                self.assertIn(key, refused.reason)
                self.assertIsInstance(fixture.connect(wire.BuildHistoryRequest()).replies()[0], wire.BuildHistoryReply)
                fixture.release(grant)
                for local in local_requests:
                    local_grant = fixture.server.ledger[local.episode].reply
                    assert isinstance(local_grant, wire.Admitted)
                    fixture.release(local_grant)
                self.assertFalse(fixture.server.reservations)

    def test_view_and_mine_keep_version_one_readings_plain_with_a_cloud_gate(self) -> None:
        with AdmissionFixture() as fixture:
            _ = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            sent: list[str] = []

            def capture(line: str) -> str:
                sent.append(line)
                return line

            with mock.patch.object(fixture.server, "line_filter", side_effect=capture):
                for version in (2, 1, 2, 1):
                    for kind, request in (("view", wire.ViewRequest()), ("mine", wire.MineRequest("cloud-session"))):
                        with self.subTest(version=version, kind=kind):
                            line = wire.encode_record({"v": version, "type": kind, "data": request}) + "\n"
                            reply, = fixture.connect(line).replies()
                            assert isinstance(reply, (wire.ViewReply, wire.MineReply))
                            message = cast(dict[str, object], json.loads(sent[-1]))
                            self.assertEqual(message["v"], version)
                            if version == 1:
                                self.assertIs(type(reply.reading), records.QueueRead)
                                data = cast(dict[str, object], message["data"])
                                old_reading = wire.parse_record(json.dumps(data["reading"]), records.QueueRead)
                                self.assertEqual(old_reading, reply.reading)
                            else:
                                self.assertIsInstance(reply.reading, wire.CloudQueueRead)
                                assert isinstance(reply.reading, wire.CloudQueueRead)
                                self.assertEqual(len(reply.reading.cloud), 1)

    def test_bad_admission_settings_hold_admission_and_keep_the_service_available(self) -> None:
        for key, invalid_values, repaired_value in (
            ("cloud_gate_slots", ("bad", "0"), "9"),
            ("cloud_gate_stale_pushes", ("bad", "0"), "3"),
            ("peak_history_share_percent", ("bad", "70"), "10"),
        ):
            for value in invalid_values:
                with self.subTest(key=key, value=value), AdmissionFixture() as fixture:
                    grant = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
                    config = fixture.root / "build_admission.conf"
                    write_memory_config(config, **{key: value})
                    fixture.server.next_pass = fixture.clock.monotonic()
                    fixture.server.run_once(0)
                    self.assertIsInstance(fixture.server.configuration, gate.UnusableAdmissionConfiguration)
                    reply, = fixture.connect(wire.ViewRequest()).replies()
                    self.assertIsInstance(reply, wire.Refused)
                    assert isinstance(reply, wire.Refused)
                    self.assertIn(key, reply.reason)
                    fixture.release(grant)
                    self.assertFalse(fixture.server.reservations)
                    write_memory_config(config, **{key: repaired_value})
                    fixture.server.run_once(0)
                    self.assertIsInstance(fixture.server.configuration, gate.UsableAdmissionConfiguration)
                    self.assertIsInstance(fixture.connect(wire.ViewRequest()).replies()[0], wire.ViewReply)

    def reading(self, fixture: AdmissionFixture) -> records.QueueRead:
        reply, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        return reply.reading

    def cloud_request(self, fixture: AdmissionFixture, *, step: str = "merge-gate",
                      run: wire.RunIdentity | wire.NoRun = wire.NO_RUN) -> wire.AdmitRequest:
        return replace(fixture.request(need=40 * GIB, step=step, run=run), place=wire.CloudPlace("aws"))

    def test_cloud_starts_with_full_local_memory_and_charges_zero(self) -> None:
        for step in ("merge-gate", "targeted-test"):
            with self.subTest(step=step), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                _ = fixture.run_pass()
                before = self.reading(fixture)
                request = self.cloud_request(fixture, step=step)
                grant = only_admitted(fixture.server.handle_request(request))
                self.assertEqual(grant.need, 0)
                self.assertEqual(fixture.server.reservations[request.episode].need, 0)
                after = self.reading(fixture)
                self.assertEqual(after.view.promised, before.view.promised)
                self.assertEqual(after.explanation.room, before.explanation.room)

    def test_cloud_module_progress_updates_without_a_memory_charge(self) -> None:
        from scripts.build_queue import cloud_progress

        with AdmissionFixture() as fixture:
            request = self.cloud_request(fixture)
            sidecar = fixture.root / "cloud.cgroup"
            _ = sidecar.write_text("")
            request = replace(request, sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            before = self.reading(fixture)
            for done, state in ((3, "running"), (12, "uploading")):
                current = cloud_progress.CloudProgress(
                    cast(cloud_progress.CloudState, state), 32, 1, fixture.clock.now())
                cloud_progress.publish(sidecar, current)
                counter = progress.ProgressCounter("modules", done, 12)
                progress.publish_counter(sidecar, counter)
                _ = fixture.run_pass()
                reading = self.reading(fixture)
                assert isinstance(reading, wire.CloudQueueRead)
                row, = reading.cloud
                self.assertEqual(row.progress, current)
                self.assertEqual(row.counter, counter)
                self.assertEqual(reading.explanation.room, before.explanation.room)
                self.assertEqual(fixture.server.reservations[request.episode].need, 0)
            fixture.release(grant)

    def test_tenth_cloud_gate_waits_and_starts_after_a_release(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            requests = [self.cloud_request(fixture) for _ in range(11)]
            grants = [only_admitted(fixture.server.handle_request(request)) for request in requests[:9]]
            for request in requests[9:]:
                reply, = fixture.server.handle_request(request)
                assert isinstance(reply, wire.Wait)
                self.assertIn("cloud slot", reply.text)
                self.assertIn("9 of 9", reply.text)
            self.assertEqual(len(fixture.server.reservations), 9)
            fixture.release(grants[0])
            _ = fixture.run_pass()
            self.assertIn(requests[9].episode, fixture.server.reservations)
            self.assertNotIn(requests[10].episode, fixture.server.reservations)
            self.assertEqual(len(fixture.server.reservations), 9)
            fixture.release(grants[1])
            _ = fixture.run_pass()
            self.assertIn(requests[10].episode, fixture.server.reservations)

    def test_cloud_slot_limit_reads_configuration_at_use(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, cloud_gate_slots="1")
            _ = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            waiting = self.cloud_request(fixture)
            reply, = fixture.server.handle_request(waiting)
            assert isinstance(reply, wire.Wait)
            self.assertIn("1 of 1", reply.text)
            write_memory_config(config, cloud_gate_slots="2")
            _ = fixture.run_pass()
            self.assertIn(waiting.episode, fixture.server.reservations)
            self.assertEqual(len(fixture.server.reservations), 2)

    def test_dead_route_releases_the_cloud_slot_while_its_child_lives(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", cloud_gate_slots="1")
            route = fixture.run_owner()
            running = self.cloud_request(fixture, run=route)
            _ = only_admitted(fixture.server.handle_request(running))
            waiting = self.cloud_request(fixture)
            reply, = fixture.server.handle_request(waiting)
            self.assertIsInstance(reply, wire.Wait)
            fixture.end_run(route)
            _ = fixture.run_pass()
            self.assertNotIn(running.episode, fixture.server.reservations)
            self.assertIn(waiting.episode, fixture.server.reservations)
            self.assertEqual(fixture.proc.owner_start(running.owner.pid), service.LiveOwnerIdentity(running.owner.start_time))

    def test_dead_cloud_owner_releases_its_slot(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", cloud_gate_slots="1")
            running = self.cloud_request(fixture)
            _ = only_admitted(fixture.server.handle_request(running))
            waiting = self.cloud_request(fixture)
            self.assertIsInstance(fixture.server.handle_request(waiting)[0], wire.Wait)
            fixture.end_owner(running.owner)
            _ = fixture.run_pass()
            self.assertNotIn(running.episode, fixture.server.reservations)
            self.assertIn(waiting.episode, fixture.server.reservations)

    def test_waiting_cloud_gate_does_not_block_a_local_step(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", cloud_gate_slots="1")
            _ = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            cloud_waiter = self.cloud_request(fixture)
            self.assertIsInstance(fixture.server.handle_request(cloud_waiter)[0], wire.Wait)
            local = fixture.request(need=GIB)
            grant = only_admitted(fixture.server.handle_request(local))
            self.assertEqual(grant.need, GIB)
            self.assertIn(cloud_waiter.episode, fixture.server.waiters)

    def test_local_memory_wait_does_not_block_a_cloud_gate(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            local = fixture.request(need=GIB)
            self.assertIsInstance(fixture.server.handle_request(local)[0], wire.Wait)
            cloud = self.cloud_request(fixture)
            grant = only_admitted(fixture.server.handle_request(cloud))
            self.assertEqual(grant.need, 0)
            self.assertIn(local.episode, fixture.server.waiters)

    def test_fallback_releases_cloud_and_waits_for_local_memory(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            cloud = self.cloud_request(fixture)
            grant = only_admitted(fixture.server.handle_request(cloud))
            fixture.release(grant)
            local = fixture.request(need=4 * GIB, step="merge-gate")
            reply, = fixture.server.handle_request(local)
            self.assertIsInstance(reply, wire.Wait)
            self.assertFalse(fixture.server.reservations)
            self.assertIn(local.episode, fixture.server.waiters)


class RoomAndSlotTests(unittest.TestCase):

    def test_an_oversized_head_starts_alone_but_unreadable_host_or_pressure_holds_it(self) -> None:
        for blocked in ("clear", "pressure", "unreadable pressure", "unreadable host"):
            with self.subTest(blocked=blocked), AdmissionFixture() as fixture:
                fixture.free(GIB)
                if blocked == "pressure":
                    fixture.hold_starts()
                elif blocked == "unreadable pressure":
                    fixture.hold_starts(unreadable=True)
                elif blocked == "unreadable host":
                    (fixture.root / "meminfo").unlink()
                request = fixture.request(need=100 * GIB)
                replies = fixture.server.handle_request(request)
                if blocked == "clear":
                    grant = only_admitted(replies)
                    self.assertEqual(fixture.server.reservations[request.episode].need, 100 * GIB)
                    self.assertEqual(grant.need, 100 * GIB)
                    result = fixture.server.last_result
                    assert isinstance(result, gate.PassResult)
                    self.assertIsInstance(result.rows[0].decision.room.basis, records.StartsAlone)
                    self.assertIn("starts alone", queue_evidence.room_check_text(result.rows[0].decision.room))
                else:
                    self.assertIsInstance(replies[0], wire.Wait)
                    self.assertFalse(fixture.server.reservations)

    def test_existing_host_reading_gets_calm_pressure_without_erasing_deliberate_pressure(self) -> None:
        for blocked in (False, True):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _ = fake_memory_readings(root)
                pressure = root / "pressure"
                if blocked:
                    write_pressure_reading(pressure, full_avg10=20.0)
                else:
                    pressure.unlink()
                with AdmissionFixture(root=root) as fixture:
                    reply = fixture.server.handle_request(fixture.request(need=12 * GIB))[0]
                    if blocked:
                        assert isinstance(reply, wire.Wait)
                        assert isinstance(reply.reason, records.HeadAdmissionStatus)
                        assert isinstance(reply.reason.cause, records.MemoryPressure)
                        self.assertEqual(reply.reason.cause.full_avg10, 20.0)
                        self.assertFalse(fixture.server.reservations)
                    else:
                        self.assertIsInstance(reply, wire.Admitted)

    def test_a_standalone_admission_prevents_another_alone_start(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request(need=6 * GIB)
            _ = only_admitted(fixture.server.handle_request(first))
            fixture.clock.advance(5)
            fixture.free(GIB)
            second = fixture.request(need=100 * GIB)
            self.assertIsInstance(fixture.server.handle_request(second)[0], wire.Wait)
            self.assertEqual(set(fixture.server.reservations), {first.episode})

    def test_new_starts_are_five_seconds_apart_without_a_pressure_event(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request(need=GIB)
            _ = only_admitted(fixture.server.handle_request(first))
            second = fixture.request(need=GIB)
            self.assertIsInstance(fixture.server.handle_request(second)[0], wire.Wait)
            fixture.clock.advance(4.999)
            _ = fixture.run_pass()
            self.assertNotIn(second.episode, fixture.server.reservations)
            fixture.clock.advance(.001)
            _ = fixture.run_pass()
            self.assertIn(second.episode, fixture.server.reservations)

    def test_a_launch_start_spaces_the_next_new_run(self) -> None:
        with AdmissionFixture() as fixture:
            launch = fixture.launch(need=GIB)
            reply, = fixture.server.handle_request(launch)
            self.assertIsInstance(reply, wire.LaunchAdmitted)
            deadline = fixture.clock.now() + admission_policy_fixture().timing.recovery_start_seconds
            self.assertEqual(fixture.server.spacing, records.NextStartAt(deadline))
            request = fixture.request(need=GIB)
            waiting, = fixture.server.handle_request(request)
            assert isinstance(waiting, wire.Wait)
            assert isinstance(waiting.reason, records.HeadAdmissionStatus)
            self.assertEqual(waiting.reason.cause, records.StartSpacingWait(deadline))
            fixture.clock.advance(4.999)
            _ = fixture.run_pass()
            self.assertNotIn(request.episode, fixture.server.reservations)
            fixture.clock.advance(.001)
            _ = fixture.run_pass()
            self.assertIn(request.episode, fixture.server.reservations)

    def test_head_protection_and_a_held_claim_show_the_walks_remaining_room(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.release(grant)
            fixture.allow_next_start()
            fixture.free(10 * GIB)
            head = fixture.request(need=2 * GIB)
            initial, = fixture.server.handle_request(head)
            assert isinstance(initial, wire.Wait)
            assert isinstance(initial.reason, records.HeadAdmissionStatus)
            self.assertEqual(initial.reason.decision.room.room, GIB)
            self.assertIn("room 1.0 GiB", records.head_wait_text(initial.reason, ZoneInfo("UTC")))
            successor = fixture.request(run=run, need=8 * GIB)
            _ = fixture.server.handle_request(successor)
            result = fixture.run_pass()
            held = fixture.server.waiters[head.episode].last_reply
            assert isinstance(held, wire.Wait)
            self.assertIsInstance(held.reason, records.BehindHead)
            protected = next(row for row in result.rows if row.owner ==
                f"{head.owner.pid}-{head.owner.start_time}")
            self.assertEqual(protected.decision.room.room, -GIB)
            claim = fixture.server.waiters[successor.episode].last_reply
            assert isinstance(claim, wire.Wait)
            assert isinstance(claim.reason, records.HeadAdmissionStatus)
            self.assertIsInstance(claim.reason.cause, records.NeedsMemory)
            self.assertEqual(claim.reason.decision.room.room, GIB)
            with mock.patch.object(client, "CLOCK", fixture.clock), mock.patch("sys.stderr", new=io.StringIO()) as output:
                client.WaitOutput()(claim)
            self.assertIn("head: needs 8.0 GiB; room 1.0 GiB", output.getvalue())
            inputs = fixture.server.last_inputs
            policy = fixture.server.last_policy
            assert isinstance(inputs, records.QueueInputs)
            assert isinstance(policy, records.AdmissionPolicy)
            reading = gate.queue_read_from_pass(inputs, policy, result)
            explained = reading.explanation.head
            assert isinstance(explained, records.HeadExplanation)
            self.assertEqual(explained.room, claim.reason.decision.room)
            self.assertEqual(explained.cause, claim.reason.cause)
            self.assertEqual([row.decision.state for row in result.rows], ["hold", "hold"])

    def test_a_held_head_keeps_one_cause_in_the_wait_line_view_and_client(self) -> None:
        for hold, status, client_reason in (
            ("pressure", "pressure", "memory pressure"),
            ("spacing", "spacing", "start spacing"),
            ("memory", "low room", "free memory"),
            ("expired", "estimate expired", "estimate expired"),
            ("unreadable", "memory reading unavailable", "memory reading unavailable"),
        ):
            with self.subTest(hold=hold), AdmissionFixture() as fixture:
                _ = only_admitted(fixture.server.handle_request(fixture.request(need=6 * GIB)))
                if hold != "spacing":
                    fixture.allow_next_start()
                if hold == "pressure":
                    fixture.hold_starts()
                elif hold == "memory":
                    fixture.free(10 * GIB)
                elif hold == "unreadable":
                    (fixture.root / "meminfo").unlink()
                request = fixture.request(need=8 * GIB, age=600 if hold == "expired" else 0)
                reply, = fixture.server.handle_request(request)
                assert isinstance(reply, wire.Wait)
                assert isinstance(reply.reason, records.HeadAdmissionStatus)
                viewed, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(viewed, wire.ViewReply)
                explanation = viewed.reading.explanation
                head = explanation.head
                assert isinstance(head, records.HeadExplanation)
                self.assertEqual(head.cause, reply.reason.cause)
                self.assertEqual(head.waiter.reason, reply.reason)
                if hold == "unreadable":
                    client_reason = f"cannot read MemAvailable from {fixture.root / 'meminfo'}"
                    status = client_reason
                self.assertEqual(reply.text, client_reason)
                with mock.patch.object(client, "CLOCK", fixture.clock), mock.patch("sys.stderr", new=io.StringIO()) as output:
                    client.WaitOutput()(reply)
                self.assertIn(records.head_wait_text(reply.reason, user_zone()), output.getvalue())
                model = queue_view.QueueModel(viewed.reading, {}, head_facts.read_head_facts(viewed.reading),
                    head_facts.PaceUnavailable(), user_zone(), queue_view.HostLoadUnavailable(),
                    gate.smallest_head_release(explanation))
                self.assertIn(status, plain([queue_view.head_status(model)]))
                self.assertEqual(client.wait_reasons(reply.snapshot), (client_reason,))

    def test_unreadable_pressure_keeps_its_path_across_the_card_client_and_wrapped_view(self) -> None:
        for unreadable in (True, False):
            with self.subTest(unreadable=unreadable), AdmissionFixture() as fixture:
                path = fixture.root / "pressure"
                if unreadable:
                    _ = path.write_text("full avg10=0\n")
                else:
                    write_pressure_reading(path, full_avg10=20)
                reply, = fixture.server.handle_request(fixture.request())
                assert isinstance(reply, wire.Wait)
                assert isinstance(reply.reason, records.HeadAdmissionStatus)
                viewed, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(viewed, wire.ViewReply)
                explanation = viewed.reading.explanation
                model = queue_view.QueueModel(viewed.reading, {}, head_facts.read_head_facts(viewed.reading),
                    head_facts.PaceUnavailable(), user_zone(), queue_view.HostLoadUnavailable(),
                    gate.smallest_head_release(explanation))
                text = plain(queue_view.render_view(model, 60))
                card = plain(queue_card.render_card(model, [1], 400))
                with mock.patch.object(client, "CLOCK", fixture.clock), mock.patch("sys.stderr", new=io.StringIO()) as output:
                    client.WaitOutput()(reply)
                if unreadable:
                    expected = f"pressure unreadable: {path} (incomplete)"
                    self.assertEqual(reply.reason.cause, records.PressureUnavailable(expected))
                    self.assertIn(expected, card)
                    self.assertIn(expected, output.getvalue())
                    self.assertIn(" pressure unreadable", text)
                    head_lines = text[text.index("Head:"):]
                    self.assertIn(str(path), "".join(line.removeprefix("· ").strip() for line in head_lines.splitlines()))
                    self.assertIn("incomplete", head_lines)
                    self.assertGreater(len(head_lines.splitlines()), 1)
                else:
                    self.assertIsInstance(reply.reason.cause, records.MemoryPressure)
                    self.assertIn(" pressure", text)
                    self.assertNotIn("pressure unreadable", text)

    def test_view_release_and_admit_pass_capture_the_same_continuation_slot(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.allow_next_start()
            other = only_admitted(fixture.server.handle_request(fixture.request(need=GIB)))
            fixture.free(0)
            successor = fixture.request(run=run, need=8 * GIB)
            _ = fixture.server.handle_request(successor)
            fixture.release(grant)
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            viewed, = reply.reading.explanation.decided_from.slots
            self.assertIsInstance(viewed, records.ContinuationSlot)
            assert isinstance(viewed, records.ContinuationSlot)
            self.assertEqual(viewed.owner, f"{successor.owner.pid}-{successor.owner.start_time}")
            fixture.release(other)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(inputs.slots, (viewed,))
            result = fixture.run_pass()
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(inputs.slots, (viewed,))
            continuation = next(row for row in result.rows if row.owner == viewed.owner)
            self.assertTrue(continuation.decision.reused_slot)
            self.assertEqual(continuation.decision.room.need, 2 * GIB)


    def test_slot_reuse_starts_under_pressure_without_order_or_spacing(self) -> None:
        for need in (4 * GIB, 6 * GIB):
            for unreadable in (False, True):
                with self.subTest(need=need, unreadable=unreadable), AdmissionFixture() as fixture:
                    run = fixture.run_owner()
                    first = fixture.request(run=run, need=6 * GIB)
                    grant = only_admitted(fixture.server.handle_request(first))
                    fixture.release(grant)
                    fixture.hold_starts(unreadable=unreadable)
                    head = fixture.request(need=GIB)
                    self.assertIsInstance(fixture.server.handle_request(head)[0], wire.Wait)
                    successor = fixture.request(run=run, need=need)
                    next_grant = only_admitted(fixture.server.handle_request(successor))
                    result = fixture.server.last_result
                    assert isinstance(result, gate.PassResult)
                    reused = next(row for row in result.rows if row.owner ==
                        f"{successor.owner.pid}-{successor.owner.start_time}")
                    self.assertIsInstance(reused.decision.room.basis, records.WithinSlot)
                    shown = queue_evidence.room_check_text(reused.decision.room)
                    self.assertIn("within held slot", shown)
                    self.assertNotIn("starts alone", shown)
                    self.assertNotEqual(next_grant.reservation, grant.reservation)
                    self.assertEqual(set(fixture.server.reservations), {successor.episode})
                    self.assertEqual(fixture.server.reservations[successor.episode].need, need)
                    self.assertFalse(fixture.server.slots)
                    self.assertIn(head.episode, fixture.server.waiters)
                    fixture.release(next_grant)
                    slot, = fixture.server.slots.values()
                    self.assertEqual(gate.between_steps(slot).bytes, need)

    def test_forecast_and_release_keep_slots_only_for_caller_named_runs(self) -> None:
        from scripts.build_queue.scope_activity import LiveBuildRun

        for caller_named in (False, True):
            with self.subTest(caller_named=caller_named), AdmissionFixture() as fixture:
                run = fixture.run_owner()
                owner = fixture.proc.spawn()
                write_process(fixture.proc.path, owner.pid, parent=run.pid, start=owner.start_time)
                _ = (fixture.proc.path / str(run.pid) / "cmdline").write_bytes(b"bash\0verify.sh\0")
                request = fixture.request(owner=owner, need=6 * GIB, run=run if caller_named else wire.NO_RUN)
                with fixture.loop_turn():
                    grant = only_admitted(fixture.server.handle_request(request))
                    _ = fixture.server.run_pass()
                    inputs = cast(records.QueueInputs, fixture.server.last_inputs)
                    self.assertIsInstance(inputs.reservations[0].get("build_run"), LiveBuildRun)
                    predicted = gate.inputs_after_steps_end(inputs, frozenset({0}), admission_policy_fixture())
                    fixture.release(grant)
                    self.assertEqual(predicted.slots, tuple(fixture.server.slots.values()))
                    if caller_named:
                        self.assertEqual(len(predicted.slots), 1)
                        self.assertEqual(gate.between_steps(predicted.slots[0]).bytes, 6 * GIB)
                    else:
                        self.assertEqual(predicted.slots, ())

    def test_a_larger_next_step_keeps_its_slot_and_gets_extra_before_the_head(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.release(grant)
            fixture.clock.advance(5)
            fixture.free(10 * GIB)
            fixture.hold_starts()
            head = fixture.request(need=GIB)
            self.assertIsInstance(fixture.server.handle_request(head)[0], wire.Wait)
            successor = fixture.request(run=run, need=8 * GIB)
            self.assertIsInstance(fixture.server.handle_request(successor)[0], wire.Wait)
            fixture.free(10 * GIB)
            _ = fixture.run_pass()
            self.assertNotIn(successor.episode, fixture.server.reservations)
            fixture.free(11 * GIB)
            _ = fixture.run_pass()
            self.assertIn(successor.episode, fixture.server.reservations)
            self.assertNotIn(head.episode, fixture.server.reservations)
            self.assertEqual(fixture.server.reservations[successor.episode].need, 8 * GIB)

    def test_a_gap_expires_at_ten_seconds_and_a_late_step_needs_a_new_start(self) -> None:
        for elapsed, reuse in ((9.999, True), (10, False)):
            with self.subTest(elapsed=elapsed), AdmissionFixture() as fixture:
                run = fixture.run_owner()
                first = fixture.request(run=run, need=6 * GIB)
                grant = only_admitted(fixture.server.handle_request(first))
                fixture.release(grant)
                fixture.clock.advance(elapsed)
                fixture.hold_starts()
                successor = fixture.request(run=run, need=4 * GIB)
                replies = fixture.server.handle_request(successor)
                if reuse:
                    self.assertEqual(only_admitted(replies).need, 4 * GIB)
                else:
                    self.assertIsInstance(replies[0], wire.Wait)
                    self.assertFalse(fixture.server.reservations)

    def test_an_ended_run_cannot_reuse_its_held_slot(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.release(grant)
            fixture.end_run(run)
            fixture.hold_starts()
            successor = fixture.request(run=run, need=4 * GIB)
            self.assertIsInstance(fixture.server.handle_request(successor)[0], wire.Wait)
            self.assertFalse(fixture.server.reservations)

    def test_restart_drops_a_gap_instead_of_restoring_memory_charge(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.release(grant)
            fixture.restart()
            fixture.hold_starts()
            successor = fixture.request(run=run, need=4 * GIB)
            self.assertIsInstance(fixture.server.handle_request(successor)[0], wire.Wait)
            self.assertFalse(fixture.server.reservations)
            self.assertNotIn('"slots"', fixture.state_text())

    def test_closing_a_waiter_connection_frees_its_claim_while_the_run_lives(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.release(grant)
            fixture.hold_starts()
            successor = fixture.request(run=run, need=8 * GIB)
            peer = fixture.connect(successor)
            self.assertTrue(any(isinstance(reply, wire.Wait) for reply in peer.replies()))
            fixture.proc.kill(successor.owner)
            peer.close()
            fixture.server.run_once(0)
            self.assertNotIn(successor.episode, fixture.server.waiters)
            self.assertIsInstance(fixture.proc.owner_start(run.pid), service.LiveOwnerIdentity)
            fixture.clock.advance(10)
            fixture.server.run_once(0)
            later = fixture.request(run=run, need=4 * GIB)
            self.assertIsInstance(fixture.server.handle_request(later)[0], wire.Wait)
            self.assertFalse(fixture.server.reservations)

    def test_a_younger_run_cannot_take_the_room_protected_for_the_head(self) -> None:
        with AdmissionFixture() as fixture:
            active = fixture.request(need=6 * GIB, owner=fixture.proc.spawn(used=0))
            _ = only_admitted(fixture.server.handle_request(active))
            fixture.clock.advance(5)
            fixture.free(15 * GIB)
            head = fixture.request(need=7 * GIB)
            self.assertIsInstance(fixture.server.handle_request(head)[0], wire.Wait)
            younger = fixture.request(need=2 * GIB)
            self.assertIsInstance(fixture.server.handle_request(younger)[0], wire.Wait)
            self.assertEqual(set(fixture.server.reservations), {active.episode})
            fixture.free(16 * GIB)
            _ = fixture.run_pass()
            self.assertIn(head.episode, fixture.server.reservations)
            self.assertNotIn(younger.episode, fixture.server.reservations)

    def test_idle_services_do_not_block_the_alone_start(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.compiler(GIB // 10)
            fixture.free(GIB)
            request = fixture.request(need=100 * GIB)
            grant = only_admitted(fixture.server.handle_request(request))
            self.assertEqual(grant.need, 100 * GIB)
            self.assertEqual(set(fixture.server.reservations), {request.episode})

    def test_the_1222_mend_head_starts_before_three_merge_gates(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.hold_starts()
            mend = fixture.request(need=int(7.3 * GIB), step="mend")
            self.assertIsInstance(fixture.server.handle_request(mend)[0], wire.Wait)
            fixture.clock.advance(1)
            gates = [fixture.request(need=6 * GIB, step="merge-gate") for _ in range(3)]
            for request in gates:
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            fixture.free(11 * GIB)
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {mend.episode})
            self.assertEqual(set(fixture.server.waiters), {request.episode for request in gates})


class QueueLifecycleTests(unittest.TestCase):
    @staticmethod
    def interrupt_append(kind: str) -> Callable[[Path, dict[str, object], float], None]:
        append = gate.append_queue_time

        def append_then_stop(directory: Path, event: dict[str, object], now: float) -> None:
            append(directory, event, now)
            if event.get("kind") == kind:
                raise InjectedCrash("after " + kind + " append")

        return append_then_stop

    def events(self, fixture: AdmissionFixture, kind: str) -> list[dict[str, object]]:
        path = fixture.state_dir / "queue_times.jsonl"
        if not path.exists():
            return []
        with path.open("rb") as stream:
            return [row for row in reversed(list(gate.queue_times_backward(stream))) if row.get("kind") == kind]

    def test_gate_admission_keeps_original_arrival_and_uncut_need(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request(step="merge-gate", since=fixture.clock.now() - 30)
            self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            fixture.clock.advance(10)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            granted = only_admitted([fixture.server.ledger[request.episode].reply])
            rows = self.events(fixture, "admitted")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual((row["pid"], row["start_time"], row["since"]),
                             (request.owner.pid, request.owner.start_time, request.since))
            self.assertEqual(row["reservation_id"], granted.reservation)
            self.assertEqual(row["admitted_at"], fixture.clock.now())
            self.assertEqual(row["wait_seconds"], 40)
            self.assertEqual(row["need"], request.profile.peak.bytes)
            self.assertEqual(row["need_source"], request.profile.peak.source)
            self.assertNotIn("budget", row)
            self.assertTrue(row["is_head"])

    def test_admission_recovery_keeps_one_event_for_the_durable_grant(self) -> None:
        for boundary in ("after_state_write", "after_admitted_append"):
            with self.subTest(boundary=boundary), AdmissionFixture() as fixture:
                request = fixture.request(step="merge-gate", since=fixture.clock.now() - 7)
                fixture.fault.stage = boundary
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(request)
                admitted_at = fixture.clock.now()
                fixture.clock.advance(10)
                fixture.restart()
                grant = only_admitted(fixture.server.handle_request(request))
                if request.episode not in fixture.server.reservations:
                    fixture.allow_next_start()
                self.assertEqual(only_admitted(fixture.server.handle_request(request)), grant)
                rows = self.events(fixture, "admitted")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reservation_id"], grant.reservation)
                self.assertEqual(rows[0]["admitted_at"], admitted_at)
                self.assertEqual(rows[0]["since"], request.since)
                self.assertEqual(rows[0]["wait_seconds"], 7)

    def test_gate_exit_before_a_positive_sample_still_records_its_end(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="merge-gate")
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.clock.advance(3)
            _ = fixture.server.handle_request(wire.ReleaseRequest("gate-end", grant.reservation, 0))
            rows = self.events(fixture, "ended")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reservation_id"], grant.reservation)
            self.assertEqual(rows[0]["since"], request.since)
            self.assertEqual(rows[0]["ended_at"], fixture.clock.now())
            self.assertEqual(rows[0]["end_kind"], {"kind": "StepEnd"})
            self.assertEqual(rows[0]["peak"], {"kind": "NoPositiveSample"})
            self.assertFalse(fixture.history())

    def test_token_return_recovery_publishes_one_end_for_each_reservation(self) -> None:
        for boundary in ("after_finalization_intent", "after_history_append"):
            with self.subTest(boundary=boundary), AdmissionFixture() as fixture:
                request = fixture.request()
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(grant)
                fixture.clock.advance(2)
                token = wire.TokenWaitRequest("token-crash", grant.reservation)
                fixture.fault.stage = boundary
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(token)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(token), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(token), [wire.Ok()])
                rows = self.events(fixture, "ended")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reservation_id"], grant.reservation)
                self.assertEqual(rows[0]["end_kind"], {"kind": "TokenReturn"})
                fixture.clock.advance(3)
                again = replace(request, episode="recovered", token_back_at=fixture.clock.now())
                if again.episode not in fixture.server.reservations:
                    fixture.allow_next_start()
                later = only_admitted(fixture.server.handle_request(again))
                self.assertNotEqual(later.reservation, grant.reservation)
                self.assertEqual(len(self.events(fixture, "admitted")), 2)
                lifetime, = gate.read_queue_lifetimes(fixture.state_dir)
                self.assertEqual((lifetime.pid, lifetime.start_time, lifetime.since),
                                 (request.owner.pid, request.owner.start_time, request.since))
                self.assertEqual([interval.reservation_id for interval in lifetime.intervals],
                                 [grant.reservation, later.reservation])
                self.assertEqual(len(lifetime.token_gaps), 1)
                self.assertIsInstance(lifetime.end, gate.StepStillOpen)

    def test_an_end_append_retry_preserves_the_saved_time_and_publishes_once(self) -> None:
        for sampled in (False, True):
            with self.subTest(sampled=sampled), AdmissionFixture() as fixture:
                request = fixture.request(step="merge-gate")
                if sampled:
                    request = replace(request, sidecar=str(fixture.scope(request.owner)))
                grant = only_admitted(fixture.server.handle_request(request))
                if sampled:
                    fixture.scoped_peak(grant, 2 * GIB)
                fixture.clock.advance(5)
                ended_at = fixture.clock.now()
                release = wire.ReleaseRequest("end-crash", grant.reservation, 137)
                with mock.patch.object(gate, "append_queue_time", side_effect=self.interrupt_append("ended")):
                    with self.assertRaises(InjectedCrash):
                        _ = fixture.server.handle_request(release)
                fixture.clock.advance(100)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                rows = self.events(fixture, "ended")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reservation_id"], grant.reservation)
                self.assertEqual(rows[0]["ended_at"], ended_at)
                self.assertEqual(rows[0]["outcome"], "killed")
                self.assertEqual(rows[0]["end_kind"], {"kind": "StepEnd"})
                self.assertEqual(len(fixture.history()), int(sampled))

    def test_cancellation_keeps_its_end_after_the_owner_exits(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request(step="merge-gate")
            peer = fixture.connect(request)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            fixture.clock.advance(10)
            cancelled_at = fixture.clock.now()
            peer.close()
            fixture.server.run_once(0)
            fixture.clock.advance(100)
            fixture.end_owner(request.owner)
            fixture.restart()
            rows = self.events(fixture, "left")
            self.assertEqual([(row["reason"], row["ended_at"]) for row in rows],
                             [("cancelled", cancelled_at)])
            lifetime, = gate.read_queue_lifetimes(fixture.state_dir)
            self.assertEqual(lifetime.end, gate.LeftQueue(cancelled_at, "cancelled"))
            self.assertEqual(lifetime.memory_wait_s, 10)
            self.assertFalse(self.events(fixture, "ended"))

    def test_restart_after_reservation_removal_keeps_the_completed_end(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="merge-gate")
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.clock.advance(5)
            ended_at = fixture.clock.now()
            clear = service.StateIntents.clear

            def clear_then_stop(intents: service.StateIntents, row: records.Reservation) -> None:
                clear(intents, row)
                raise InjectedCrash("after reservation removal")

            with mock.patch.object(service.StateIntents, "clear", new=clear_then_stop):
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("clear-stop", grant.reservation, 0))
            fixture.proc.kill(request.owner)
            fixture.clock.advance(100)
            fixture.restart()
            self.assertFalse(self.events(fixture, "left"))
            rows = self.events(fixture, "ended")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reservation_id"], grant.reservation)
            lifetime, = gate.read_queue_lifetimes(fixture.state_dir)
            self.assertEqual(lifetime.end,
                gate.ReservationEnded(ended_at, "completed", gate.StepEnd(), gate.NoPositiveSample()))

    def test_restart_retries_an_interrupted_cancellation_with_its_original_time(self) -> None:
        for appended in (False, True):
            with self.subTest(appended=appended), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request(step="merge-gate")
                peer = fixture.connect(request)
                self.assertIsInstance(peer.replies()[0], wire.Wait)
                fixture.clock.advance(10)
                cancelled_at = fixture.clock.now()
                failure = self.interrupt_append("left") if appended else InjectedCrash("before cancellation append")
                with mock.patch.object(gate, "append_queue_time", side_effect=failure):
                    peer.close()
                    with self.assertRaises(InjectedCrash):
                        fixture.server.run_once(0)
                fixture.clock.advance(100)
                fixture.restart()
                fixture.end_owner(request.owner)
                fixture.restart()
                rows = self.events(fixture, "left")
                self.assertEqual([(row["reason"], row["ended_at"]) for row in rows],
                                 [("cancelled", cancelled_at)])
                lifetime, = gate.read_queue_lifetimes(fixture.state_dir)
                self.assertEqual(lifetime.end, gate.LeftQueue(cancelled_at, "cancelled"))
                self.assertEqual(lifetime.memory_wait_s, 10)
                self.assertFalse(self.events(fixture, "ended"))


    def test_a_waiting_owner_exit_and_socket_cancel_record_distinct_left_reasons(self) -> None:
        for reason in ("cancelled", "owner_gone"):
            with self.subTest(reason=reason), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request(step="merge-gate")
                peer = fixture.connect(request)
                self.assertIsInstance(peer.replies()[0], wire.Wait)
                fixture.clock.advance(4)
                if reason == "cancelled":
                    peer.close()
                    fixture.server.run_once(0)
                else:
                    fixture.end_owner(request.owner)
                rows = self.events(fixture, "left")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reason"], reason)
                self.assertEqual((rows[0]["pid"], rows[0]["start_time"], rows[0]["since"]),
                                 (request.owner.pid, request.owner.start_time, request.since))
                self.assertFalse(self.events(fixture, "ended"))
                self.assertFalse(fixture.history())


class CiSliceAdmissionTests(unittest.TestCase):

    def test_ci_worker_uses_the_current_reservation_and_waiter_identity(self) -> None:
        from scripts.build_queue.scope_activity import IdleService, RunningServiceJob, WaitingServiceJob, UnmatchedServiceJob

        for admitted, expected in ((True, RunningServiceJob), (False, WaitingServiceJob)):
            with self.subTest(admitted=admitted), AdmissionFixture() as fixture:
                owner = fixture.fixture_owner(700001, "100")
                write_process(fixture.proc.path, 700010)
                _ = (fixture.proc.path / "700010/comm").write_text("Runner.Worker\n")
                write_process(fixture.proc.path, owner.pid, parent=700010, start=owner.start_time)
                _ = (fixture.root / "ci.slice/cgroup.procs").write_text(f"700010\n{owner.pid}\n")
                if not admitted:
                    _ = only_admitted(fixture.server.handle_request(
                        fixture.request(owner=fixture.fixture_owner(700020, "200"), need=GIB)))
                request = fixture.request(owner=owner, need=GIB if admitted else 100 * GIB)
                reply = fixture.server.handle_request(request)
                self.assertEqual(any(isinstance(item, wire.Admitted) for item in reply), admitted)
                _ = fixture.run_pass()
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                ci, = (step.measured for step in view.reading.view.running
                       if isinstance(step.measured, records.UnownedSliceMemory)
                       and step.measured.cgroup == str(fixture.root / "ci.slice"))
                self.assertIsInstance(ci.service_job_state, expected)
                self.assertIsInstance(ci.job_state, IdleService if admitted else RunningServiceJob)
                self.assertEqual(len(view.reading.view.waiting), int(not admitted))
                if admitted:
                    grant = only_admitted(reply)
                    fixture.release(grant)
                else:
                    fixture.server.waiters.clear()
                _ = fixture.run_pass()
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                ci, = (step.measured for step in view.reading.view.running
                       if isinstance(step.measured, records.UnownedSliceMemory)
                       and step.measured.cgroup == str(fixture.root / "ci.slice"))
                self.assertIsInstance(ci.service_job_state, UnmatchedServiceJob)
                self.assertIsInstance(ci.job_state, RunningServiceJob)

    def test_ci_evidence_does_not_subtract_host_free_memory_twice(self) -> None:
        for shared, ci_bytes, run_bytes in ((0, 4, 0), (4, 0, 4)):
            with self.subTest(shared=shared), AdmissionFixture() as fixture:
                _ = (fixture.root / "ci.slice/memory.current").write_text(str(4 * GIB))
                _ = (fixture.root / "ci.slice/memory.stat").write_text(
                    f"anon {(4 - shared) * GIB}\nshmem {shared * GIB}\ninactive_file 0\n")
                request = fixture.request(need=18 * GIB)
                if shared:
                    sidecar = fixture.scope(request.owner, folder="ci.slice/job.scope", current=4 * GIB, anonymous=0)
                    scope = Path(sidecar.read_text())
                    _ = (scope / "memory.stat").write_text(f"anon 0\nshmem {4 * GIB}\ninactive_file 0\n")
                    _ = (fixture.root / "ci.slice/cgroup.procs").write_text(str(request.owner.pid))
                    request = replace(request, sidecar=str(sidecar))
                grant = only_admitted(fixture.server.handle_request(request))
                self.assertEqual(grant.need, 18 * GIB)
                if shared:
                    _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                ci, = (step for step in reply.reading.view.running
                       if isinstance(step.measured, records.UnownedSliceMemory)
                       and step.measured.cgroup == str(fixture.root / "ci.slice"))
                assert isinstance(ci.measured, records.UnownedSliceMemory)
                self.assertEqual(ci.measured.bytes, ci_bytes * GIB)
                room = reply.reading.explanation.room
                assert isinstance(room, records.MemoryRoom)
                self.assertEqual(room.available, 60 * GIB)
                self.assertEqual(room.used, run_bytes * GIB)
                self.assertEqual(room.ci_use, ci_bytes * GIB)
                self.assertEqual(room.cap, (60 + ci_bytes + run_bytes - 3) * GIB)

    def test_missing_ci_limit_does_not_block_grants_or_cleanup(self) -> None:
        with AdmissionFixture() as fixture:
            (fixture.root / "ci.slice/memory.max").unlink()
            request = fixture.request(need=GIB)
            grant = only_admitted(fixture.server.handle_request(request))
            self.assertEqual(fixture.server.handle_request(
                wire.ReleaseRequest("ci-cleanup", grant.reservation, 0)), [wire.Ok()])
            self.assertNotIn(request.episode, fixture.server.reservations)


class ServiceTests(unittest.TestCase):

    def test_caller_admission_sets_the_child_zone(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = {"BUILDLOG_DIR": str(root / "log"), "USER_ZONE": "Pacific/Pago_Pago"}
        _ = caller_admission(self, root, environment)
        self.assertEqual(environment["USER_ZONE"], "UTC")
        self.assertEqual(environment["USER_ZONE"], os.environ["USER_ZONE"])

    def test_fixture_uses_utc_and_restores_the_callers_zone(self) -> None:
        for zone in ("Pacific/Pago_Pago", ""):
            with self.subTest(zone=zone), mock.patch.dict(os.environ):
                if zone:
                    os.environ["USER_ZONE"] = zone
                else:
                    _ = os.environ.pop("USER_ZONE", None)
                with AdmissionFixture():
                    self.assertEqual(os.environ["USER_ZONE"], "UTC")
                self.assertEqual(os.environ.get("USER_ZONE", ""), zone)
                self.assertEqual("USER_ZONE" in os.environ, bool(zone))

    def test_each_pass_publishes_captured_readings_and_remaining_waiters(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request()
            _ = fixture.server.handle_request(request)
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            self.assertIsInstance(snapshot, queue_snapshot.QueueSnapshot)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(snapshot.inputs.host, inputs.host)
            self.assertEqual(snapshot.inputs.pressure, inputs.pressure)
            self.assertEqual(snapshot.inputs.running, inputs.running)
            self.assertEqual(snapshot.inputs.reservations, inputs.reservations)
            self.assertEqual([row for _, row in snapshot.inputs.ordered],
                             [{**waiting.row, "order_since": waiting.row["since"]}
                              for waiting in fixture.server.waiters.values()])
            self.assertEqual(snapshot.written_at, fixture.clock.now())
            self.assertEqual(snapshot.policy, fixture.server.last_policy)
            fixture.clock.advance(1)
            fixture.free(60 * GIB)
            counts = fixture.readings.counts.copy()
            _ = fixture.server.run_pass()
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.written_at, fixture.clock.now())
            self.assertEqual(snapshot.inputs.ordered, [])
            self.assertIn(request.episode, fixture.server.reservations)
            self.assertEqual(fixture.readings.counts["host"] - counts["host"], 1)
            self.assertEqual(fixture.readings.counts["pressure"] - counts["pressure"], 1)
            self.assertEqual(fixture.readings.counts["compilers"] - counts["compilers"], 1)

    def test_snapshot_keeps_priority_metadata_outside_waiter_rows(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first, second = fixture.request(), fixture.request()
            labels = {first.identity.session_id: UnitLabels("first", unit="other"),
                      second.identity.session_id: UnitLabels("second", unit="urgent")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                _ = fixture.server.handle_request(first)
                _ = fixture.server.handle_request(second)
                _ = fixture.completed_queue_change(wire.PrioritySetRequest("ranked-snapshot", "urgent", "urgent", "director"))
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            rows = [row for _, row in snapshot.inputs.ordered]
            self.assertEqual(rows, [fixture.server.waiters[request.episode].row for request in (second, first)])
            for row in rows:
                self.assertNotIn("order", row)
                self.assertNotIn("priority_rank", row)
            self.assertEqual([records.waiter_priority(row) for _, row in gate.ranked_queue_waiters(snapshot.inputs)],
                             [records.PriorityUnit("urgent"), records.NormalOrder()])
            assert isinstance(fixture.server.last_policy, records.AdmissionPolicy)
            reading = gate.queue_read_from(snapshot.inputs, snapshot.policy)
            self.assertEqual([row["pid"] for _, row in reading.ordered], [second.owner.pid, first.owner.pid])

    def test_snapshot_replacement_failure_keeps_the_last_complete_snapshot(self) -> None:
        with AdmissionFixture() as fixture:
            _ = fixture.server.run_pass()
            previous = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            policy = fixture.server.last_policy
            assert isinstance(policy, records.AdmissionPolicy)
            with mock.patch.object(os, "replace", side_effect=OSError("snapshot unavailable")):
                with self.assertRaises(OSError):
                    queue_snapshot.write_queue_snapshot(fixture.state_dir, replace(inputs, now=inputs.now + 1),
                                                        policy)
            self.assertEqual(queue_snapshot.read_queue_snapshot(fixture.state_dir), previous)
            self.assertFalse((fixture.state_dir / "queue.json.tmp").exists())
            with mock.patch.object(queue_snapshot, "write_queue_snapshot", side_effect=OSError("snapshot unavailable")):
                _ = fixture.server.run_pass()
            self.assertEqual(queue_snapshot.read_queue_snapshot(fixture.state_dir), previous)

    def test_snapshot_accepts_unowned_memory_without_a_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            unowned = fixture.root / "builds.slice/unowned.scope"
            unowned.mkdir(parents=True)
            _ = (unowned / "memory.current").write_text(str(GIB))
            _ = (unowned / "memory.stat").write_text("inactive_file 0\n")
            _ = (unowned / "cgroup.procs").write_text("")
            _ = fixture.server.run_pass()
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.inputs, fixture.server.last_inputs)
            self.assertTrue(any(isinstance(step.measured, records.UnownedSliceMemory) for step in snapshot.inputs.running))

    def test_unreadable_owner_refuses_without_waiter_or_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            with mock.patch.object(fixture.proc, "owner_start", return_value=service.OwnerProcessGone()):
                self.assertEqual(fixture.server.handle_request(request), [wire.Refused("owner no longer exists")])
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)

    def test_socket_fixture_works_with_a_long_tmpdir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="admit-long-", dir="/tmp") as root:
            long_tmpdir = Path(root) / ("nested-" * 24)
            long_tmpdir.mkdir()
            with mock.patch.dict(os.environ, {"TMPDIR": str(long_tmpdir)}), \
                 mock.patch.object(tempfile, "tempdir", str(long_tmpdir)), \
                 AdmissionFixture() as fixture:
                self.assertEqual(fixture.root.parent, long_tmpdir)
                self.assertGreater(len(os.fsencode(fixture.root / "admission.sock")), 108)
                self.assertLess(len(os.fsencode(fixture.socket_path)), 108)
                self.assertTrue(fixture.socket_path.is_socket())
                peer = fixture.connect(wire.ViewRequest())
                self.assertTrue(any(isinstance(reply, wire.ViewReply) for reply in peer.replies()))

    def test_restored_override_grace_starts_at_listener_readiness_once_for_both_sockets(self) -> None:
        for inherited in (False, True):
            with self.subTest(inherited=inherited), AdmissionFixture() as fixture:
                fixture.configure(override_reconnect_seconds=4, reconnect_seconds=2)
                fixture.free(0)
                fixture.hold_starts()
                with mock.patch.object(fixture.readings, "pressure", return_value=records.MemoryPressure(10, condition="full")):
                    _ = fixture.server.handle_request(fixture.request())
                    _ = fixture.server.handle_request(wire.AdmitHeadRequest("delayed-head", "urgent", "director"))
                    override = fixture.server.override
                    self.assertNotIsInstance(override, records.NoHeadAdmissionOverride)
                    fixture.server.close()
                    owner_start = fixture.proc.owner_start

                    def slow_restore(pid: int) -> service.LiveOwnerIdentity | service.OwnerProcessGone:
                        fixture.clock.advance(10)
                        return owner_start(pid)

                    with mock.patch.object(fixture.proc, "owner_start", side_effect=slow_restore):
                        fixture.server = fixture.new_server(start=False)
                    self.assertGreater(fixture.clock.monotonic(), 4)
                    self.assertEqual(fixture.server.override, override)
                    self.assertIsInstance(fixture.server.override_reconnect_until, service.ReconnectAwaitingListener)
                    fixture.clock.advance(10)
                    _ = fixture.server.run_pass()
                    self.assertEqual(fixture.server.override, override)
                    # Keep the last read settings until another service pass.
                    fixture.configure(override_reconnect_seconds=1, reconnect_seconds=6)
                    ready_at = fixture.clock.monotonic()
                    if inherited:
                        socket_type = socket.socket
                        with socket_type(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                            listener.bind(str(fixture.socket_path))
                            listener.listen()

                            def inherited_socket(*args: object, **kwargs: object) -> socket.socket:
                                self.assertEqual(args, ())
                                self.assertEqual(kwargs, {"fileno": 3})
                                return socket_type(fileno=os.dup(listener.fileno()))

                            with mock.patch.dict(os.environ, {"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid())}), \
                                    mock.patch.object(socket, "socket", side_effect=inherited_socket):
                                fixture.server.start()
                    else:
                        fixture.server.start()
                    self.assertEqual(fixture.server.override_reconnect_until, service.ReconnectDeadline(ready_at + 4))
                    self.assertEqual(fixture.server.next_pass, min(ready_at + 4,
                        ready_at + records.read_memory_config().running_pass_seconds))
                    fixture.clock.advance(3.9)
                    fixture.server.start()
                    self.assertEqual(fixture.server.override_reconnect_until, service.ReconnectDeadline(ready_at + 4))
                    fixture.server.run_once(0)
                    self.assertEqual(fixture.server.override, override)
                    fixture.clock.advance(0.1)
                    fixture.server.run_once(0)
                    self.assertIsInstance(fixture.server.override, records.NoHeadAdmissionOverride)
                    self.assertIsInstance(wire.parse_record(fixture.state_text(), service.ServiceState).override,
                                          records.NoHeadAdmissionOverride)

    def test_pressure_held_launch_reconnect_resolves_override_after_promotion_once(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(override_reconnect_seconds=4)
            request = fixture.launch()
            with mock.patch.object(fixture.readings, "pressure", return_value=records.MemoryPressure(10, condition="full")):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
                _ = fixture.server.handle_request(wire.AdmitHeadRequest("launch-head", "urgent", "director"))
                override = fixture.server.override
                self.assertNotIsInstance(override, records.NoHeadAdmissionOverride)
                fixture.restart()
                fixture.clock.advance(3.9)
                admission_pass = gate.admission_pass

                def pass_crossing_deadline(inputs: records.QueueInputs, now: float,
                                           policy: records.AdmissionPolicy) -> gate.PassResult:
                    self.assertEqual(inputs.override, override)
                    self.assertEqual(len(inputs.ordered), 1)
                    fixture.clock.advance(1)
                    return admission_pass(inputs, now, policy)

                with mock.patch.object(gate, "admission_pass", side_effect=pass_crossing_deadline) as passed:
                    self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
                self.assertEqual(passed.call_count, 1)
                self.assertEqual(fixture.server.override, override)
                self.assertIsInstance(fixture.server.override_reconnect_until, service.NoReconnectGrace)
            _ = fixture.server.run_pass()
            self.assertIsInstance(fixture.server.ledger[request.episode].reply, wire.LaunchAdmitted)

    def test_queued_replay_is_read_before_expiring_restored_override(self) -> None:
        for already_accepted in (False, True):
            with self.subTest(already_accepted=already_accepted), AdmissionFixture() as fixture:
                fixture.configure(override_reconnect_seconds=4)
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request()
                with mock.patch.object(fixture.readings, "pressure", return_value=records.MemoryPressure(10, condition="full")):
                    _ = fixture.server.handle_request(request)
                    _ = fixture.server.handle_request(wire.AdmitHeadRequest("queued-head", "urgent", "director"))
                    override = fixture.server.override
                    self.assertNotIsInstance(override, records.NoHeadAdmissionOverride)
                    fixture.restart()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                        peer.connect(str(fixture.socket_path))
                        if already_accepted:
                            fixture.server.run_once(0)
                            self.assertEqual(len(fixture.server.connections), 1)
                        peer.sendall(wire.encode_request(request).encode())
                        fixture.clock.advance(4)
                        fixture.server.run_once(0)
                        self.assertIn(request.episode, fixture.server.waiters)
                        self.assertEqual(fixture.server.override, override)
                        self.assertIsInstance(fixture.server.override_reconnect_until, service.NoReconnectGrace)

    def test_deadline_drains_large_replays_but_expires_for_incomplete_or_unrelated_traffic(self) -> None:
        for traffic in ("large-new", "large-existing", "incomplete", "unrelated"):
            with self.subTest(traffic=traffic), AdmissionFixture() as fixture:
                fixture.configure(override_reconnect_seconds=4)
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request()
                request = replace(request, argv=(*request.argv, "x" * 70000))
                with mock.patch.object(fixture.readings, "pressure", return_value=records.MemoryPressure(10, condition="full")):
                    _ = fixture.server.handle_request(request)
                    _ = fixture.server.handle_request(wire.AdmitHeadRequest("large-head", "urgent", "director"))
                    override = fixture.server.override
                    self.assertNotIsInstance(override, records.NoHeadAdmissionOverride)
                    fixture.restart()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                        peer.connect(str(fixture.socket_path))
                        if traffic == "large-existing":
                            fixture.server.run_once(0)
                            self.assertEqual(len(fixture.server.connections), 1)
                        encoded = wire.encode_request(request).encode()
                        self.assertGreater(len(encoded), 65536)
                        if traffic == "incomplete":
                            encoded = encoded[:-1]
                        elif traffic == "unrelated":
                            encoded = wire.encode_request(wire.ViewRequest()).encode()
                        peer.sendall(encoded)
                        fixture.clock.advance(4)
                        fixture.server.run_once(0)
                        if traffic.startswith("large-"):
                            self.assertIn(request.episode, fixture.server.waiters)
                            self.assertEqual(fixture.server.override, override)
                        else:
                            self.assertNotIn(request.episode, fixture.server.waiters)
                            self.assertIsInstance(fixture.server.override, records.NoHeadAdmissionOverride)
                        self.assertIsInstance(fixture.server.override_reconnect_until, service.NoReconnectGrace)

    def test_second_request_and_oversized_input_end_admission_before_next_pass(self) -> None:
        for violation in ("second-request", "oversize-newline", "oversize-incomplete"):
            with self.subTest(violation=violation), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request()
                if violation == "second-request":
                    peer = fixture.connect(request)
                    self.assertIsInstance(peer.replies()[0], wire.Wait)
                    connection = next(iter(fixture.server.connections.values()))
                    original_wait = fixture.server.waiters[request.episode].last_reply
                    assert isinstance(original_wait, wire.Wait)
                    peer.socket.sendall(wire.encode_request(wire.ViewRequest()).encode())
                else:
                    encoded = wire.encode_request(request)
                    if violation == "oversize-incomplete":
                        encoded = encoded[:-1]
                    fixture.configure(request_max_bytes=len(encoded.encode()) - 1)
                    peer = fixture.connect(encoded)
                    # Accept and read it during the next receive phase.
                    connection = None
                    original_wait = None
                # A due pass can admit the step as soon as reception completes.
                fixture.free(60 * GIB)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertNotIn(request.episode, fixture.server.waiters)
                self.assertNotIn(request.episode, fixture.server.reservations)
                self.assertNotIn(request.episode, fixture.server.ledger)
                if connection is None:
                    connection = next(iter(fixture.server.connections.values()))
                self.assertTrue(connection.finished)
                if original_wait is not None:
                    sending = connection.sending
                    fixture.server.send_wait(connection, original_wait)
                    self.assertTrue(connection.finished)
                    self.assertEqual(connection.sending, sending)
                replies = peer.replies()
                self.assertEqual(len(replies), 1)
                refused = replies[0]
                self.assertIsInstance(refused, wire.Refused)
                assert isinstance(refused, wire.Refused)
                self.assertIn("one request" if violation == "second-request" else "exceeds", refused.reason)
                self.assertEqual(fixture.server.connections, {})
                self.assertEqual(peer.socket.recv(1), b"")
                with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed:
                    for _ in range(3):
                        self.assertEqual(fixture.server.epoll.poll(0), [])
                        fixture.server.run_once(0)
                    self.assertEqual(passed.call_count, 0)

    def test_scope_that_empties_after_lifetime_capture_ends_in_the_same_pass(self) -> None:
        for stage in ("restore", "pending ending", "memory reading"):
            with self.subTest(stage=stage), AdmissionFixture() as fixture:
                request = fixture.request()
                sidecar = fixture.scope(request.owner)
                scope = Path(sidecar.read_text())
                request = replace(request, sidecar=str(sidecar))
                with fixture.loop_turn():
                    grant = only_admitted(fixture.server.handle_request(request))
                    fixture.scoped_peak(grant)
                    fixture.proc.kill(request.owner)
                    (fixture.proc.path / str(request.owner.pid) / "stat").unlink()
                    if stage != "memory reading":
                        fixture.server.reservations[request.episode] = replace(
                            fixture.server.reservations[request.episode], ending=service.OwnerExited())
                        fixture.server.persist()
                    lifetime = fixture.proc.lifetime
                    captures = 0
                    def empty_after_capture(row: records.Reservation, owner_live: bool) -> gate.StepLifetime:
                        nonlocal captures
                        captured = lifetime(row, owner_live)
                        if isinstance(captured, gate.PopulatedStepScope):
                            captures += 1
                            _ = (scope / "cgroup.procs").write_text("")
                        return captured
                    with mock.patch.object(fixture.proc, "lifetime", side_effect=empty_after_capture):
                        if stage == "restore":
                            fixture.restart()
                        else:
                            _ = fixture.server.run_pass()
                    self.assertEqual(captures, 1)
                    self.assertNotIn(request.episode, fixture.server.reservations)
                    self.assertEqual(len(fixture.history()), 1)

    def test_scope_watch_closes_on_release_and_reused_scope_is_watched_again(self) -> None:
        with AdmissionFixture() as fixture:
            events = fixture.scope_events()
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            _ = fixture.server.run_pass()
            descriptor = next(fd for fd, watch in fixture.server.watchers.items()
                              if isinstance(watch, service.ScopeWatch))
            watch = fixture.server.watchers[descriptor]
            assert isinstance(watch, service.ScopeWatch)
            self.assertEqual(watch.episode, request.episode)
            _ = fixture.server.handle_request(wire.ReleaseRequest("release-scope", grant.reservation, 0))
            self.assertIn(descriptor, events.unregistered)
            with self.assertRaises(OSError):
                _ = os.fstat(descriptor)
            fixture.clock.advance(0.001)
            second = replace(request, episode="scope-reused", sidecar=str(fixture.scope(request.owner)))
            if second.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            _ = only_admitted(fixture.server.handle_request(second))
            _ = fixture.server.run_pass()
            self.assertEqual(len(events.registered), 2)
            scoped = [watch for watch in fixture.server.watchers.values() if isinstance(watch, service.ScopeWatch)]
            self.assertEqual(scoped, [service.ScopeWatch(watch.path, second.episode)])

    def test_removed_scope_wakeup_closes_watch_and_continues_the_loop(self) -> None:
        for error in (errno.ENODEV, errno.ENOENT):
            with self.subTest(error=error), AdmissionFixture() as fixture:
                events = fixture.scope_events()
                request = fixture.request()
                sidecar = fixture.scope(request.owner)
                request = replace(request, sidecar=str(sidecar))
                _ = only_admitted(fixture.server.handle_request(request))
                _ = fixture.server.run_pass()
                descriptor = next(fd for fd, watch in fixture.server.watchers.items()
                                  if isinstance(watch, service.ScopeWatch))
                scope = Path(sidecar.read_text())
                (scope / "cgroup.procs").unlink()
                (scope / "cgroup.events").unlink()
                original_read = os.read
                def removed_read(fd: int, size: int) -> bytes:
                    if fd == descriptor:
                        raise OSError(error, "scope removed")
                    return original_read(fd, size)
                events.events.append((descriptor, select.EPOLLERR))
                fixture.readings.counts.clear()
                with mock.patch.object(os, "read", side_effect=removed_read):
                    fixture.server.run_once(0)
                self.assertIn(descriptor, events.unregistered)
                self.assertNotIn(descriptor, fixture.server.watchers)
                self.assertEqual(fixture.readings.counts["host"], 1)
                self.assertFalse(fixture.server.stopped)

    def test_zero_wait_socket_restart_recovers_after_sigkill_and_rejects_a_live_listener(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.kill_socket_server()
            self.assertTrue(fixture.socket_path.exists())
            fixture.server = fixture.new_server()
            peer = fixture.connect(wire.ViewRequest())
            self.assertIsInstance(peer.replies()[0], wire.ViewReply)
            contender = service.AdmissionServer(fixture.state_dir, fixture.clock, fixture.readings,
                fixture.proc, socket_path=fixture.socket_path)
            try:
                with self.assertRaisesRegex(RuntimeError, "already listening"):
                    contender.start()
                self.assertTrue(fixture.socket_path.exists())
            finally:
                contender.close()

    def test_socket_permissions_are_private_at_bind_before_listen(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            bind = socket.socket.bind
            modes: list[int] = []
            def observe_bind(listener: socket.socket, address: str) -> None:
                bind(listener, address)
                modes.append(stat.S_IMODE(Path(address).stat().st_mode))
            previous = os.umask(0)
            try:
                with mock.patch.object(socket.socket, "bind", observe_bind):
                    fixture.server = fixture.new_server()
            finally:
                _ = os.umask(previous)
            self.assertEqual(modes, [0o600])

    def test_pressure_held_override_survives_restart_until_its_waiter_reconnects(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(override_reconnect_seconds=4)
            _ = only_admitted(fixture.server.handle_request(fixture.request(need=12 * GIB)))
            fixture.free(16 * GIB)
            request = fixture.request(need=12 * GIB)
            mutation = wire.AdmitHeadRequest("persisted-head", "urgent", "director")
            with mock.patch.object(fixture.readings, "pressure", return_value=records.MemoryPressure(10, condition="full")):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
                self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
                override = fixture.server.override
                self.assertNotIsInstance(override, records.NoHeadAdmissionOverride)
                fixture.restart()
                self.assertEqual(fixture.server.override, override)
                fixture.clock.advance(3)
                fixture.server.run_once(0)
                self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
                self.assertEqual(fixture.server.override, override)
                _ = fixture.server.handle_request(request)
                self.assertEqual(fixture.server.override, override)
                saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                self.assertEqual(saved.override, override)
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            _ = fixture.server.run_pass()
            self.assertIsInstance(fixture.server.ledger[request.episode].reply, wire.Admitted)

    def test_restored_override_lapses_after_configured_reconnect_chance(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(override_reconnect_seconds=4)
            fixture.free(0)
            fixture.hold_starts()
            with mock.patch.object(fixture.readings, "pressure", return_value=records.MemoryPressure(10, condition="full")):
                _ = fixture.server.handle_request(fixture.request())
                _ = fixture.server.handle_request(wire.AdmitHeadRequest("head-grace", "urgent", "director"))
                fixture.restart()
                fixture.clock.advance(3.9)
                fixture.server.run_once(0)
                self.assertNotIsInstance(fixture.server.override, records.NoHeadAdmissionOverride)
                fixture.clock.advance(0.1)
                fixture.server.run_once(0)
                self.assertIsInstance(fixture.server.override, records.NoHeadAdmissionOverride)
                self.assertIsInstance(wire.parse_record(fixture.state_text(), service.ServiceState).override,
                                      records.NoHeadAdmissionOverride)

    def test_mutation_refusals_replay_durably_after_state_changes(self) -> None:
        with AdmissionFixture() as fixture:
            empty = wire.AdmitHeadRequest("refused-head", "urgent", "director")
            refused = fixture.server.handle_request(empty)
            fixture.restart()
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            self.assertEqual(fixture.server.handle_request(empty), refused)
            self.assertIsInstance(fixture.server.override, records.NoHeadAdmissionOverride)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            fixture.allow_next_start()
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            width = wire.WidthRequest("refused-width", grant.reservation, 0)
            refused_width = fixture.server.handle_request(width)
            self.assertIsInstance(refused_width[0], wire.Refused)
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(replace(width, width=2)), refused_width)
            self.assertEqual(fixture.server.reservations[request.episode].test_width, request.test_width)

    def test_width_replays_live_through_retention_then_expire_after_release(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(replay_retention_seconds=2)
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            old = wire.WidthRequest("old-width", grant.reservation, 4)
            _ = fixture.server.handle_request(old)
            _ = fixture.server.handle_request(replace(old, request_id="new-width", width=2))
            fixture.clock.advance(3)
            _ = fixture.server.run_pass()
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(old), [wire.Ok()])
            self.assertEqual(fixture.server.reservations[request.episode].test_width, records.RequestedTestWidth(2))
            _ = fixture.server.handle_request(wire.ReleaseRequest("end-width", grant.reservation, 0))
            fixture.clock.advance(1)
            _ = fixture.server.run_pass()
            self.assertIn(old.request_id, fixture.server.ledger)
            fixture.clock.advance(2)
            _ = fixture.server.run_pass()
            self.assertNotIn(old.request_id, fixture.server.ledger)

    def test_admission_and_width_update_freeze_the_matching_history_need_and_margin(self) -> None:
        with mock.patch.object(store, "host_name", return_value="test-host"), AdmissionFixture() as fixture:
            fixture.configure(test_phase_margin=1.4)
            request = fixture.request(need=12 * GIB)
            assert isinstance(request.profile, records.StepProfile)
            request = replace(request, profile=replace(request.profile,
                repository=records.RepositoryIdentity("fixture-repository", "fixture")))
            for width, peak in ((8, 3 * GIB), (4, 2 * GIB)):
                store.append_line(fixture.state_dir / "anon_peaks.jsonl", {
                    "host": "test-host", "repo_identity": "fixture-repository", "step": "nextest",
                    "test_threads": width, "test_phase_peak_bytes": peak,
                    "ended_at": store.utc_iso(fixture.clock.now() - 1)})
            grant = only_admitted(fixture.server.handle_request(request))
            captured = fixture.server.reservations[request.episode].test_phase_need
            self.assertEqual(captured, records.TestPhaseNeed(3 * GIB, 1, 8, 8, "same_width", margin=1.4))
            fixture.configure(test_phase_margin=1.6)
            _ = fixture.run_pass()
            self.assertEqual(fixture.server.reservations[request.episode].test_phase_need, captured)
            self.assertEqual(fixture.server.handle_request(wire.WidthRequest("width", grant.reservation, 4)), [wire.Ok()])
            updated = fixture.server.reservations[request.episode]
            self.assertEqual(updated.test_phase_need, records.TestPhaseNeed(2 * GIB, 1, 4, 4, "same_width", margin=1.6))
            self.assertEqual(updated.need, 12 * GIB)

    def test_width_update_preserves_accepted_need_and_measured_test_phase_history(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=11 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 3 * GIB)
            record = fixture.server.reservations[request.episode]
            phase = gate.test_phase_path(service.reservation_row(record))
            assert isinstance(phase, Path)
            _ = phase.write_text(wire.encode_record({"test_phase_peak_bytes": 3 * GIB,
                "test_phase_started_at": fixture.clock.now(), "test_threads": 4,
                "call_id": "call-1", "test_scope": "full", "repo_identity": "fixture",
                "package": "", "filters": []}))
            self.assertEqual(fixture.server.handle_request(wire.WidthRequest("width", grant.reservation, 4)), [wire.Ok()])
            changed = fixture.server.reservations[request.episode]
            self.assertEqual(changed.need, 11 * GIB)
            self.assertEqual(changed.test_width, records.RequestedTestWidth(4))
            if request.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            self.assertEqual(only_admitted(fixture.server.handle_request(request)), grant)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("end", grant.reservation, 0)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual(row["need"], 11 * GIB)
            self.assertEqual(row["test_threads"], 4)
            self.assertEqual(row["test_phase_peak_bytes"], 3 * GIB)

    def test_promoted_launch_and_input_composition_share_waiter_ordering(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            launch = fixture.launch(since=fixture.clock.now() - 10)
            _ = fixture.server.handle_request(launch)
            with fixture.server.observe_waiter_ordering() as ordered:
                later = fixture.request()
                _ = fixture.server.handle_request(later)
            self.assertGreaterEqual(ordered.call_count, 2)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual([row["pid"] for _, row in inputs.ordered], [launch.owner.pid, later.owner.pid])

    def test_launch_hold_reason_comes_from_the_shared_reason_rule(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            reason = records.EstimateExpired(fixture.clock.now())
            with mock.patch.object(gate, "queue_wait_reason", return_value=reason) as explain:
                reply = fixture.server.handle_request(fixture.launch())[0]
            assert isinstance(reply, wire.Wait)
            assert isinstance(reply.reason, records.HeadAdmissionStatus)
            self.assertEqual(reply.reason.cause, reason)
            self.assertEqual(reply.text, queue_view.reason_phrase(reason, ()))
            self.assertEqual(explain.call_count, 1)

    def test_unchanged_passes_do_not_rewrite_durable_state(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            state = fixture.state_text()
            with mock.patch.object(service, "write_text_atomic", wraps=write_text_atomic) as write:
                for _ in range(3):
                    fixture.clock.advance(1)
                    _ = fixture.server.run_pass()
                self.assertEqual(write.call_count, 0)
                self.assertEqual(fixture.state_text(), state)
                _ = fixture.server.handle_request(fixture.request())
                self.assertGreater(write.call_count, 0)

    def test_delivered_waits_leave_only_the_current_reply_and_no_pending_history(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request()
            first = fixture.server.handle_request(request)
            self.assertIsInstance(first[0], wire.Wait)
            for available in (GIB, 2 * GIB, 0):
                fixture.free(available)
                fixture.hold_starts()
                fixture.clock.advance(60)
                _ = fixture.server.run_pass()
                self.assertIsInstance(fixture.server.waiters[request.episode].last_reply, wire.Wait)
                self.assertEqual(fixture.server.pending_replies, {})
                self.assertNotIn(request.episode, fixture.server.ledger)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            self.assertNotIn(request.episode, fixture.server.waiters)
            self.assertIsInstance(fixture.server.ledger[request.episode].reply, wire.Admitted)
            self.assertEqual(fixture.server.pending_replies, {})

    def test_wait_and_source_lifetimes_use_distinct_states(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            with fixture.server.observe_wait_initial_states() as before_delivery:
                request = fixture.request()
                _ = fixture.server.handle_request(request)
            self.assertEqual(before_delivery, [service.NoWaitReply()])
            self.assertIsInstance(fixture.server.waiters[request.episode].last_reply, wire.Wait)
            self.assertIsInstance(fixture.server.source_change_at, service.SourcesUnchanged)
            source = fixture.root / "removed-source.py"
            _ = source.write_text("loaded")
            info = source.stat()
            fixture.server.sources = {source: (info.st_ino, info.st_mtime_ns)}
            source.unlink()
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            self.assertIsInstance(fixture.server.sources[source], service.SourceMissing)
            self.assertEqual(fixture.server.source_change_at, fixture.clock.monotonic())
            self.assertTrue(all(isinstance(watch, service.OwnerWatch) for watch in fixture.server.watchers.values()))

    def test_wait_poll_and_heartbeat_intervals_follow_config_changes(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(waiting_pass_seconds=3, wait_reply_seconds=7)
            fixture.free(0)
            fixture.hold_starts()
            peer = fixture.connect(fixture.request())
            self.assertEqual(len(peer.replies()), 1)
            fixture.readings.counts.clear()
            fixture.clock.advance(2)
            self.assertFalse(peer.replies())
            self.assertEqual(fixture.readings.counts["host"], 0)
            fixture.clock.advance(1)
            self.assertFalse(peer.replies())
            self.assertEqual(fixture.readings.counts["host"], 1)
            fixture.configure(waiting_pass_seconds=1, wait_reply_seconds=4)
            fixture.clock.advance(3)
            self.assertEqual(len(peer.replies()), 1)
            self.assertEqual(fixture.server.next_pass, fixture.clock.monotonic() + 1)

    def test_source_check_and_quiet_window_follow_config(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(source_check_seconds=2, source_settle_seconds=4)
            fixture.server.next_source_check = fixture.clock.monotonic() + 2
            source = fixture.root / "loaded-config.py"
            _ = source.write_text("old")
            info = source.stat()
            fixture.server.sources = {source: (info.st_ino, info.st_mtime_ns)}
            source.unlink()
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            self.assertIsInstance(fixture.server.source_change_at, service.SourcesUnchanged)
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            fixture.clock.advance(2)
            fixture.server.run_once(0)
            self.assertFalse(fixture.server.stopped)
            fixture.clock.advance(2)
            fixture.server.run_once(0)
            self.assertTrue(fixture.server.stopped)

    def test_serve_binds_the_environment_socket_without_listen_fds(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            socket_path = fixture.socket_dir / "from-environment.sock"
            run_once = service.AdmissionServer.run_once
            def one_turn(server: service.AdmissionServer) -> None:
                run_once(server, 0)
                self.assertEqual(server.socket_path, socket_path)
                self.assertTrue(socket_path.is_socket())
                self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)
            with mock.patch.dict(os.environ, {"ADMISSION_SOCKET": str(socket_path), "LISTEN_FDS": ""}), \
                 mock.patch.object(service, "SystemClock", return_value=fixture.clock), \
                 mock.patch.object(service, "SystemReadings", return_value=fixture.readings), \
                 mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(service.AdmissionServer, "serve_forever", one_turn), \
                 mock.patch.object(sys, "argv", ["admission_service", "serve", "--state-dir", str(fixture.state_dir)]):
                self.assertEqual(service.main(), 0)
            self.assertFalse(socket_path.exists())

    def test_each_pass_matches_the_shared_rule_and_view_uses_its_frozen_limits(self) -> None:
        for free in (0, 10 * GIB, 60 * GIB):
            with self.subTest(free=free), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                requests = [fixture.request(need=need * GIB, since=fixture.clock.now() - age)
                            for need, age in ((12, 400), (4, 30), (2, 10))]
                for request in requests:
                    _ = fixture.server.handle_request(request)
                fixture.free(free)
                with mock.patch.object(gate, "admission_pass", wraps=gate.admission_pass) as run:
                    result = fixture.server.run_pass()
                assert isinstance(result, gate.PassResult)
                self.assertEqual(run.call_count, 1)
                inputs = fixture.server.last_inputs
                policy = fixture.server.last_policy
                assert isinstance(policy, records.AdmissionPolicy)
                assert isinstance(inputs, records.QueueInputs)
                self.assertEqual(result, gate.admission_pass(inputs, inputs.now, policy=policy))
                starts = sorted((row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)),
                    key=lambda row: row.admission.rank if isinstance(row.admission, gate.ProposedAdmission) else -1)
                committed = list(fixture.server.reservations.values())
                self.assertEqual([f"{record.owner.pid}-{record.owner.start_time}" for record in committed],
                                 [row.owner for row in starts])
                for row in result.rows:
                    request = next(request for request in requests if records.waiter_owner({
                        "pid": request.owner.pid, "start_time": request.owner.start_time,
                        "since": request.since, "need": request.profile.peak.bytes}) == row.owner)
                    if isinstance(row.admission, gate.NotAdmitted):
                        waiting = fixture.server.waiters[request.episode].last_reply
                        assert isinstance(waiting, wire.Wait)
                        self.assertEqual(waiting.reason, row.reason)
                        self.assertEqual(waiting.text, queue_view.reason_phrase(row.reason,
                            gate.queue_read_from(inputs, policy=policy).view.waiting))
                expected = gate.queue_read_from(inputs, policy=policy)
                self.assertEqual(fixture.server.handle_request(wire.ViewRequest()), [wire.ViewReply(expected)])
                explained = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(explained, wire.ExplainReply)
                self.assertEqual(explained.explanation.decided_from, inputs)
                self.assertEqual(explained.explanation.policy, policy)

    def test_queries_project_the_stored_pass_without_another_admission_walk(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request()
            _ = fixture.server.handle_request(request)
            inputs = fixture.server.last_inputs
            result, policy = fixture.server.last_result, fixture.server.last_policy
            assert isinstance(policy, records.AdmissionPolicy)
            assert isinstance(inputs, records.QueueInputs)
            assert isinstance(result, gate.PassResult)
            projected = gate.queue_read_from_pass(inputs, policy, result)
            self.assertEqual(projected, gate.queue_read_from(inputs, policy))
            with mock.patch.object(gate, "admission_pass", wraps=gate.admission_pass) as walk:
                viewed = fixture.server.handle_request(wire.ViewRequest())
                explained = fixture.server.handle_request(wire.ExplainRequest())
                mine = fixture.server.handle_request(wire.MineRequest(request.identity.session_id))
            walk.assert_not_called()
            self.assertEqual(viewed, [wire.ViewReply(projected)])
            self.assertEqual(explained, [wire.ExplainReply(projected.explanation)])
            own = mine[0]
            assert isinstance(own, wire.MineReply)
            self.assertEqual(own.reading.explanation.decided_from, inputs)


    def test_wait_lines_then_ranked_admissions_and_release_frees_charge(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            peers = [fixture.connect(fixture.request(need=8 * GIB)) for _ in range(2)]
            for peer in peers:
                self.assertTrue(any(isinstance(reply, wire.Wait) for reply in peer.replies()))
            fixture.free(60 * GIB)
            result = fixture.server.run_pass()
            assert isinstance(result, gate.PassResult)
            expected = sorted((row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)),
                              key=lambda row: row.admission.rank if isinstance(row.admission, gate.ProposedAdmission) else -1)
            first = only_admitted(peers[0].replies())
            self.assertTrue(any(isinstance(reply, wire.Wait) for reply in peers[1].replies()))
            fixture.allow_next_start()
            next_result = fixture.server.run_pass()
            assert isinstance(next_result, gate.PassResult)
            expected.extend(row for row in next_result.rows if isinstance(row.admission, gate.ProposedAdmission))
            grants = [first, only_admitted(peers[1].replies())]
            self.assertEqual([record.owner.pid for record in fixture.server.reservations.values()],
                             [int(row.owner.split('-')[0]) for row in expected])
            before = fixture.clock.monotonic()
            _ = fixture.server.handle_request(wire.ReleaseRequest("end-first", grants[0].reservation, 0))
            self.assertLess(fixture.clock.monotonic() - before, 0.2)
            self.assertEqual(len(fixture.server.reservations), 1)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(len(inputs.reservations), 1)

    def test_width_and_call_states_survive_restart_and_replay(self) -> None:
        cases: tuple[tuple[records.TestWidth, records.Call, str], ...] = (
            (records.RequestedTestWidth(3), test_phase.CallRecord("recorded"), "nextest"),
            (records.UnspecifiedTestWidth(), test_phase.NoCallRecord(), "nextest"),
            (records.NoTestWidth(), test_phase.NoCallRecord(), "clippy"))
        for width, call, step in cases:
            with self.subTest(width=width), AdmissionFixture() as fixture:
                request = fixture.request(width=width, call=call, step=step)
                granted = only_admitted(fixture.server.handle_request(request))
                record = fixture.server.reservations[request.episode]
                row = service.reservation_row(record)
                self.assertEqual(row["test_threads"], width)
                self.assertEqual(row["call_id"], call)
                fixture.restart()
                self.assertEqual(fixture.server.reservations[request.episode], record)
                self.assertNotIn('null', fixture.state_text())
                if request.episode not in fixture.server.reservations:
                    fixture.allow_next_start()
                self.assertEqual(only_admitted(fixture.server.handle_request(request)), granted)
                self.assertEqual(len(fixture.server.reservations), 1)

    def test_running_and_idle_views_refresh_at_the_configured_interval(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            _ = write_config_fixture(config, "running_pass_seconds=3\n")
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            granted = only_admitted(fixture.server.handle_request(request))
            for resident in (GIB, 5 * GIB):
                record = fixture.server.reservations[request.episode]
                scope = Path(Path(record.sidecar).read_text())
                _ = (scope / "memory.current").write_text(str(resident))
                fixture.clock.advance(3)
                fixture.server.run_once(0)
                reply = fixture.server.handle_request(wire.ViewRequest())[0]
                assert isinstance(reply, wire.ViewReply)
                self.assertFalse(reply.reading.view.waiting)
                step, = (step for step in reply.reading.view.running if gate.running_has_reservation(step))
                assert isinstance(step.measured, records.MeasuredMemory)
                self.assertEqual(step.measured.bytes, resident)
            _ = fixture.server.handle_request(wire.ReleaseRequest("end", granted.reservation, 0))
            fixture.readings.counts.clear()
            fixture.clock.advance(100)
            fixture.server.run_once(0)
            self.assertEqual(fixture.readings.counts["host"], 1)
            fixture.server.run_once(0)
            self.assertEqual(fixture.readings.counts["host"], 1)

    def test_supplied_readings_are_taken_once_per_pass(self) -> None:
        with AdmissionFixture() as fixture:
            _ = fixture.server.handle_request(fixture.request())
            fixture.readings.counts.clear()
            _ = fixture.server.run_pass()
            self.assertEqual(set(fixture.readings.counts.values()), {1})
            self.assertEqual(fixture.readings.counts["host"], 1)
            self.assertEqual(fixture.readings.counts["pressure"], 1)
            self.assertEqual(fixture.readings.counts["compilers"], 1)

    def test_crash_after_grant_persistence_replays_one_reservation_and_place(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            fixture.fault.stage = "after_state_write"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(request)
            fixture.restart()
            granted = only_admitted(fixture.server.handle_request(request))
            if request.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            self.assertEqual(granted, only_admitted(fixture.server.handle_request(request)))
            self.assertEqual(len(fixture.server.reservations), 1)
            self.assertEqual(len(fixture.server.places), 1)
            record = fixture.server.reservations[request.episode]
            self.assertEqual(granted.reservation, record.reservation_id)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertGreaterEqual(gate.run_memories(inputs)[0].need, request.profile.peak.bytes)

    def test_release_crashes_finish_once_from_saved_intent_or_history(self) -> None:
        for boundary in ("after_state_write", "after_finalization_intent", "after_history_append"):
            with self.subTest(boundary=boundary), AdmissionFixture() as fixture:
                request = fixture.request()
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                granted = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(granted)
                record = fixture.server.reservations[request.episode]
                release = wire.ReleaseRequest("release-crashed", granted.reservation, 137)
                if boundary == "after_finalization_intent":
                    fixture.fault.stage = boundary
                    with self.assertRaises(InjectedCrash):
                        _ = fixture.server.handle_request(release)
                    saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                    self.assertIsInstance(saved.reservations[request.episode].finalization, dict)
                    self.assertFalse(list(fixture.state_dir.glob("*.reservation")))
                    self.assertFalse(fixture.history())
                else:
                    def stop_after_durable_release(stage: str) -> None:
                        if stage != boundary:
                            return
                        saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                        if boundary == "after_state_write" and release.request_id not in saved.ledger:
                            return
                        raise InjectedCrash(boundary)
                    with mock.patch.object(fixture.server, "fault", side_effect=stop_after_durable_release):
                        with self.assertRaises(InjectedCrash):
                            _ = fixture.server.handle_request(release)
                    if boundary == "after_state_write":
                        saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                        self.assertEqual(saved.ledger[release.request_id].reply, wire.Ok())
                        self.assertFalse(saved.reservations)
                        self.assertEqual(len(fixture.history()), 1)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                rows = fixture.history()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reservation_id"], record.reservation_id)
                self.assertEqual(rows[0]["outcome"], "killed")
                self.assertFalse(Path(record.path).exists())
                self.assertFalse(Path(granted.max_path).exists())
                self.assertFalse(fixture.server.reservations)

    def test_normal_release_appends_without_reading_history_rows(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 9 * GIB)
            history = fixture.state_dir / "anon_peaks.jsonl"
            store.append_line(history, {"reservation_id": "earlier"})
            scanned = AssertionError("normal release scanned history")
            with (mock.patch.object(store, "whole_rows", side_effect=scanned),
                  mock.patch.object(store, "locked_rows", side_effect=scanned)):
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("end", grant.reservation, 0)), [wire.Ok()])
            rows = fixture.history()
            self.assertEqual([row["reservation_id"] for row in rows], ["earlier", grant.reservation])
            self.assertEqual(rows[1]["anon_peak_bytes"], 9 * GIB)
            self.assertFalse(fixture.server.reservations)

    def test_crash_recovery_finds_the_existing_record_after_the_saved_history_offset(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 9 * GIB)
            history = fixture.state_dir / "anon_peaks.jsonl"
            store.append_line(history, {"reservation_id": "earlier"})
            offset = history.stat().st_size
            fixture.fault.stage = "after_history_append"
            release = wire.ReleaseRequest("end", grant.reservation, 137)
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(release)
            saved = service.read_state(fixture.state_dir).reservations[request.episode].finalization
            assert isinstance(saved, dict)
            self.assertEqual(saved.get("history_start"), offset)
            with mock.patch.object(store, "whole_rows", wraps=store.whole_rows) as scan:
                fixture.restart()
                scan.assert_any_call(mock.ANY, offset)
            self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            rows = fixture.history()
            self.assertEqual([row["reservation_id"] for row in rows], ["earlier", grant.reservation])
            self.assertEqual((rows[1]["outcome"], rows[1]["status"]), ("killed", 137))
            self.assertFalse(fixture.server.reservations)

    def test_mutation_request_ids_replay_without_repeating_changes(self) -> None:
        with AdmissionFixture() as fixture:
            granted = only_admitted(fixture.server.handle_request(fixture.request()))
            width = wire.WidthRequest("width-once", granted.reservation, 4)
            self.assertEqual(fixture.server.handle_request(width), [wire.Ok()])
            state = fixture.state_text()
            self.assertEqual(fixture.server.handle_request(replace(width, width=2)), [wire.Ok()])
            self.assertEqual(fixture.state_text(), state)
            release = wire.ReleaseRequest("release-once", granted.reservation, 0)
            self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            state = fixture.state_text()
            self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            self.assertEqual(fixture.state_text(), state)

            for process in ("current", "fresh"):
                with self.subTest(process=process):
                    if process == "current":
                        fixture.restart()
                        restored = wire.encode_record(fixture.server.ledger[release.request_id].reply)
                    else:
                        result = subprocess.run([sys.executable, "-c", "\n".join((
                            "import sys",
                            "from scripts.build_queue import admission_protocol as wire",
                            "from scripts.build_queue.admission_test_support import AdmissionFixture",
                            "with AdmissionFixture() as fixture:",
                            "    (fixture.state_dir / 'state.json').write_text(sys.stdin.read())",
                            "    fixture.restart()",
                            "    print(wire.encode_record(fixture.server.ledger['release-once'].reply))",
                        ))], input=state, capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[2],
                            timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        restored = result.stdout.strip()
                    self.assertEqual(restored, wire.encode_record(wire.Ok()))

    def test_restart_thirty_waiters_keeps_places_and_order_fields(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            requests = [fixture.request(since=fixture.clock.now() - 600 + index) for index in range(30)]
            for request in requests:
                _ = fixture.server.handle_request(request)
            places = fixture.server.places.copy()
            fixture.restart()
            for request in reversed(requests):
                _ = fixture.server.handle_request(request)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual([row["pid"] for _, row in inputs.ordered], [request.owner.pid for request in requests])
            self.assertEqual(fixture.server.places, places)
            for _, row in gate.ranked_queue_waiters(inputs):
                self.assertEqual(records.waiter_priority(row), records.NormalOrder())
                self.assertNotIn("head_since", row)
                self.assertNotIn("head_state", row)
            for forbidden in ('"head_since"', '"head_state"', '"bump_until"', '"bump_ends_at"'):
                self.assertNotIn(forbidden, fixture.state_text())

    def test_snapshot_ranking_keeps_priority_then_run_place_then_pid_order(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            joined = fixture.clock.now() - 600
            first = fixture.request(since=joined)
            tied = fixture.request(since=joined)
            younger = fixture.request(since=joined + 1)
            for request in (younger, tied, first):
                _ = fixture.server.handle_request(request)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual([row["pid"] for _, row in inputs.ordered],
                             [first.owner.pid, tied.owner.pid, younger.owner.pid])
            rows = {row["pid"]: row for _, row in inputs.ordered}
            self.assertEqual([rows[request.owner.pid]["since"] for request in (first, tied, younger)],
                             [joined, joined, joined + 1])
            priority = gate.rank_waiter(rows[younger.owner.pid], records.PriorityUnit("urgent"))
            ranked = sorted([rows[tied.owner.pid], priority, rows[first.owner.pid]], key=gate.waiter_order)
            self.assertEqual([row["pid"] for row in ranked],
                             [younger.owner.pid, first.owner.pid, tied.owner.pid])
            self.assertEqual(priority["since"], younger.since)
            for request in (first, tied, younger):
                self.assertEqual(rows[request.owner.pid].get("order_since"), request.since)
            moved = records.stored_waiter({**rows[first.owner.pid], "order_since": younger.since + 1})
            self.assertEqual(moved.get("order_since"), younger.since + 1)
            self.assertGreater(gate.waiter_order(moved), gate.waiter_order(rows[younger.owner.pid]))

    def test_two_episodes_of_one_step_reuse_its_first_place(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request(since=fixture.clock.now() - 200)
            granted = only_admitted(fixture.server.handle_request(first))
            places = tuple(fixture.server.places.values())
            _ = fixture.server.handle_request(wire.ReleaseRequest("token-release", granted.reservation, 0))
            fixture.free(0)
            fixture.hold_starts()
            second = replace(first, episode="after-token")
            _ = fixture.server.handle_request(second)
            waiting = fixture.server.waiters[second.episode]
            self.assertEqual(waiting.row["since"], first.since)
            place = tuple(fixture.server.places.values())[0]
            self.assertEqual((place.since, place.priority_since),
                             (places[0].since, places[0].priority_since))
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            fixture.clock.advance(0.001)
            _ = fixture.server.run_pass()
            later = only_admitted([fixture.server.ledger[second.episode].reply])
            self.assertNotEqual(granted.reservation, later.reservation)
            self.assertEqual(set(fixture.server.reservations), {second.episode})

    def test_owner_exit_removes_waiter_and_unscoped_reservation_through_pidfd(self) -> None:
        for waiting in (True, False):
            with self.subTest(waiting=waiting), AdmissionFixture() as fixture:
                fixture.free(0 if waiting else 60 * GIB)
                request = fixture.request()
                _ = fixture.server.handle_request(request)
                fixture.proc.kill(request.owner)
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.waiters)
                self.assertFalse(fixture.server.reservations)
                self.assertFalse(fixture.server.places)

    def test_scope_outlives_its_shell_and_empty_scope_event_releases_charge(self) -> None:
        with AdmissionFixture() as fixture:
            owner, scope, stop = fixture.transient_scope()
            sidecar = fixture.root / "real-scope.cgroup"
            _ = sidecar.write_text(str(scope))
            request = replace(fixture.request(owner=owner), sidecar=str(sidecar))
            guard = gate.check_memory_reading
            def membership_only(path: Path) -> None:
                # cgroup.procs is lifecycle evidence; no live memory file is allowed.
                if path != scope / "cgroup.procs":
                    guard(path)
            def supplied_scope_memory(_path: Path, row: records.Reservation, _now: object,
                                      _processes: object, _proc: Path, policy: records.AdmissionPolicy) -> records.StepMemory:
                _ = policy
                lifetime = fixture.proc.lifetime(row, fixture.proc.owner_start(owner.pid) == service.LiveOwnerIdentity(owner.start_time))
                return records.GoneMemory() if isinstance(lifetime, gate.EndedStep) else records.MeasuredMemory(GIB)
            with mock.patch.object(gate, "check_memory_reading", side_effect=membership_only), \
                 mock.patch.object(fixture.readings, "reservation", side_effect=supplied_scope_memory):
                _ = only_admitted(fixture.server.handle_request(request))
                _ = fixture.server.run_pass()
                descriptor = os.pidfd_open(owner.pid)
                try:
                    os.kill(owner.pid, signal.SIGKILL)
                    self.assertTrue(select.select([descriptor], [], [], 3)[0], "owner must exit")
                finally:
                    os.close(descriptor)
                fixture.server.run_once(0)
                self.assertEqual(len(fixture.server.reservations), 1)
                stop()
                fixture.server.run_once(0)
                # Ended lifetime comes from the real empty scope, not owner polling.
                self.assertFalse(fixture.server.reservations)

    def test_closed_waiting_connection_leaves_queue_but_grant_survives_disconnect(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            peer = fixture.connect(fixture.request())
            _ = peer.replies()
            peer.close()
            fixture.server.run_once(0)
            self.assertFalse(fixture.server.waiters)
            fixture.free(60 * GIB)
            peer = fixture.connect(fixture.request())
            _ = only_admitted(peer.replies())
            peer.close()
            fixture.server.run_once(0)
            self.assertEqual(len(fixture.server.reservations), 1)

    def test_unparsed_request_refusals_decode_with_both_protocol_versions(self) -> None:
        for version in (1, 2):
            for kind in ("malformed", "fragmented", "oversize-newline", "oversize-incomplete"):
                with self.subTest(version=version, kind=kind), AdmissionFixture() as fixture:
                    fixture.configure(request_max_bytes=512)
                    _ = fixture.run_pass()
                    prefix = f'{{"v":{version},'
                    if kind.startswith("oversize"):
                        line = prefix + '"data":"'
                        tail = '","type":"view"}\n' if kind == "oversize-newline" else ""
                        limit = max(512, wire.CLEANUP_REQUEST_MAX_BYTES)
                        line += "x" * (limit + 1 - len(line) - len(tail)) + tail
                    else:
                        line = prefix if kind == "fragmented" else prefix + '"type":"view","data": }\n'
                    sent: list[str] = []

                    def capture(line: str) -> str:
                        sent.append(line)
                        return line

                    with mock.patch.object(fixture.server, "line_filter", side_effect=capture):
                        peer = fixture.connect(line)
                        if kind == "fragmented":
                            self.assertEqual(peer.replies(), [])
                            peer.socket.sendall(b'"type":"view","data": }\n')
                        reply, = peer.replies()
                    self.assertIsInstance(reply, wire.Refused)
                    self.assertEqual(codec.snapshot_value(decode_legacy(sent[-1])), codec.snapshot_value(reply))
                    self.assertEqual(wire.parse_reply(sent[-1]), reply)

    def test_unknown_version_gets_explicit_refusal_and_socket_is_private(self) -> None:
        with AdmissionFixture() as fixture:
            peer = fixture.connect('{"v":4,"type":"view","data":{}}\n')
            self.assertEqual(peer.replies(), [wire.Refused("client version 4, service supports 1..3")])
            self.assertEqual(stat.S_IMODE(fixture.socket_path.stat().st_mode), 0o600)

    def test_aged_launch_alone_does_not_create_a_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(4 * GIB)
            fixture.compiler(8 * GIB)
            request = fixture.launch(since=fixture.clock.now() - 900)
            self.assertIsInstance(fixture.server.handle_request(request)[0], wire.LaunchAdmitted)
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)
            self.assertEqual(wire.parse_record(fixture.state_text(), service.ServiceState).reservations, {})
            fixture.hold_starts()
            held, = fixture.server.handle_request(replace(request, episode="held-launch"))
            self.assertIsInstance(held, wire.Wait)
            self.assertFalse(fixture.server.reservations)

    def test_expiry_and_refresh_keep_episode_place_and_order_fields(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request(age=admission_policy_fixture().timing.estimate_refresh_seconds - 1)
            young = fixture.server.handle_request(request)[0]
            assert isinstance(young, wire.Wait)
            self.assertNotIsInstance((young.reason.cause if isinstance(young.reason, records.HeadAdmissionStatus) else young.reason), records.EstimateExpired)
            fixture.clock.advance(1)
            _ = fixture.server.run_pass()
            expired = fixture.server.waiters[request.episode].last_reply
            assert isinstance(expired, wire.Wait)
            self.assertIsInstance((expired.reason.cause if isinstance(expired.reason, records.HeadAdmissionStatus) else expired.reason), records.EstimateExpired)
            place = tuple(fixture.server.places.values())[0]
            refreshed = replace(request, profile=replace(request.profile, estimated_at=fixture.clock.now()))
            _ = fixture.server.handle_request(refreshed)
            self.assertEqual(tuple(fixture.server.places.values()), (place,))
            self.assertEqual(len(fixture.server.waiters), 1)

    def test_admit_head_records_actor_once_and_changes_the_pass(self) -> None:
        with AdmissionFixture() as fixture:
            mutation = wire.AdmitHeadRequest("override", "urgent", "director")
            self.assertEqual(fixture.server.handle_request(replace(mutation, request_id="empty-head")), [wire.Refused("nothing waits; no head to admit")])
            _ = only_admitted(fixture.server.handle_request(fixture.request(need=12 * GIB)))
            fixture.free(16 * GIB)
            request = fixture.request(need=12 * GIB)
            self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            fixture.allow_next_start()
            with mock.patch.object(gate, "head_override_event") as event:
                self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
                _ = fixture.server.handle_request(mutation)
            self.assertEqual(event.call_args_list[0].args[0], "set")
            self.assertEqual(event.call_args_list[0].args[1]["set_by"], "director")
            self.assertEqual(sum(call.args[0] == "set" for call in event.call_args_list), 1)
            self.assertIsInstance(fixture.server.ledger[request.episode].reply, wire.Admitted)
            self.assertNotIn(request.episode, fixture.server.waiters)

    def test_loaded_source_change_or_deletion_stops_after_two_quiet_seconds(self) -> None:
        for delete in (False, True):
            with self.subTest(delete=delete), AdmissionFixture() as fixture:
                source = fixture.root / "loaded.py"
                _ = source.write_text("old")
                info = source.stat()
                fixture.server.sources = {source: (info.st_ino, info.st_mtime_ns)}
                if delete:
                    source.unlink()
                else:
                    _ = source.write_text("new code")
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.stopped)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.stopped)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertTrue(fixture.server.stopped)
                self.assertTrue((fixture.state_dir / "state.json").exists())

    def test_serve_defaults_to_the_private_admission_directory(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            with mock.patch.object(sys, "argv", ["admission_service", "serve"]), \
                 mock.patch.object(service, "SystemClock", return_value=fixture.clock), \
                 mock.patch.object(service, "SystemReadings", return_value=fixture.readings), \
                 mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(service.AdmissionServer, "serve_forever") as serve:
                self.assertEqual(service.main(), 0)
            serve.assert_called_once_with()
            self.assertTrue((fixture.root / "log/admission/state.json").exists())


class CutoverServiceTests(unittest.TestCase):

    def test_admit_head_keeps_free_memory_and_pressure_guards_and_ends_only_its_own_override(self) -> None:
        for free, pressure, admitted in ((14, 0, False), (15, 0, True), (40, 6, False)):
            with self.subTest(free=free, pressure=pressure), AdmissionFixture() as fixture:
                holder = fixture.request(need=20 * GIB, owner=fixture.proc.spawn(20 * GIB))
                write_process(fixture.proc.path, holder.owner.pid, 20 * GIB,
                              start=holder.owner.start_time, anonymous=0)
                _ = only_admitted(fixture.server.handle_request(holder))
                fixture.free(free * GIB)
                fixture.allow_next_start()
                head = fixture.request(need=12 * GIB)
                follower = fixture.request(need=12 * GIB, since=head.since + 1)
                if pressure:
                    fixture.hold_starts()
                _ = fixture.server.handle_request(head)
                _ = fixture.server.handle_request(follower)
                with mock.patch.object(fixture.readings, "pressure", return_value=records.MemoryPressure(pressure, condition="full") if pressure else records.NoPressureHold()), \
                     mock.patch.object(gate, "head_override_event", wraps=gate.head_override_event) as events:
                    self.assertEqual(fixture.server.handle_request(
                        wire.AdmitHeadRequest("override", "user asked", "showrunner-id")), [wire.Ok()])
                self.assertEqual([call.args[0] for call in events.call_args_list],
                                 ["set", "admitted"] if admitted else ["set"])
                record = cast(records.HeadAdmissionOverride, events.call_args_list[0].args[1])
                self.assertEqual((record["pid"], record["start_time"], record["need"], record["free"],
                                  record["reserve"], record["why"], record["set_by"]),
                                 (head.owner.pid, head.owner.start_time, 12 * GIB, free * GIB,
                                  3 * GIB, "user asked", "showrunner-id"))
                self.assertEqual(head.episode in fixture.server.reservations, admitted)
                self.assertIsInstance(fixture.server.waiters[follower.episode].last_reply, wire.Wait)
                result = fixture.server.last_result
                assert isinstance(result, gate.PassResult)
                decision = next(row.decision for row in result.rows if row.owner == records.waiter_owner(
                    {"pid": head.owner.pid, "start_time": head.owner.start_time, "need": 12 * GIB,
                     "since": head.since}))
                self.assertEqual(decision.promised, 20 * GIB)
                self.assertEqual(isinstance(fixture.server.override, records.NoHeadAdmissionOverride), admitted)
                if pressure:
                    self.assertIsInstance(decision.pressure, records.MemoryPressure)

    def test_replacing_admit_head_replays_once_and_owner_exit_lapses_it_without_transfer(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first = fixture.request(since=fixture.clock.now() - 10)
            second = fixture.request()
            _ = fixture.server.handle_request(first)
            _ = fixture.server.handle_request(second)
            with mock.patch.object(gate, "head_override_event", wraps=gate.head_override_event) as events:
                _ = fixture.server.handle_request(wire.AdmitHeadRequest("first", "first", "director"))
                replacement = wire.AdmitHeadRequest("replacement", "replacement", "director")
                _ = fixture.server.handle_request(replacement)
                _ = fixture.server.handle_request(replacement)
                override = fixture.server.override
                assert not isinstance(override, records.NoHeadAdmissionOverride)
                self.assertEqual((override["pid"], override["start_time"], override["why"]),
                                 (first.owner.pid, first.owner.start_time, "replacement"))
                fixture.proc.kill(first.owner)
                fixture.server.run_once(0)
            self.assertEqual([call.args[0] for call in events.call_args_list], ["set", "set", "lapsed"])
            self.assertIsInstance(fixture.server.override, records.NoHeadAdmissionOverride)
            self.assertIsInstance(fixture.server.waiters[second.episode].last_reply, wire.Wait)
            self.assertNotIn(first.episode, fixture.server.waiters)

    def test_pressure_held_override_has_the_same_view_explanation_and_frozen_release_result(self) -> None:
        with AdmissionFixture() as fixture:
            _ = only_admitted(fixture.server.handle_request(fixture.request(need=20 * GIB, owner=fixture.proc.spawn(20 * GIB))))
            fixture.free(23 * GIB)
            head = fixture.request(need=12 * GIB)
            _ = fixture.server.handle_request(head)
            with mock.patch.object(fixture.readings, "pressure", return_value=records.MemoryPressure(6, condition="full")):
                _ = fixture.server.handle_request(wire.AdmitHeadRequest("override", "urgent", "director"))
            before = fixture.state_text()
            viewed = fixture.server.handle_request(wire.ViewRequest())[0]
            explained = fixture.server.handle_request(wire.ExplainRequest())[0]
            assert isinstance(viewed, wire.ViewReply) and isinstance(explained, wire.ExplainReply)
            self.assertEqual(viewed.reading.explanation, explained.explanation)
            current = explained.explanation.head
            assert isinstance(current, records.HeadExplanation)
            self.assertEqual(current.waiter.reason, viewed.reading.view.waiting[0].reason)
            self.assertEqual(current.decision.state, "hold")
            self.assertIsInstance(gate.smallest_head_release(explained.explanation), gate.HeadStillHeld)
            self.assertEqual(fixture.state_text(), before)
            self.assertIn(head.episode, fixture.server.waiters)
            self.assertNotIsInstance(fixture.server.override, records.NoHeadAdmissionOverride)

    def test_release_records_each_closed_outcome_once_and_removes_the_sampler_paths(self) -> None:
        for status, outcome in ((0, "completed"), (1, "failed"), (127, "failed"),
                                (128, "killed"), (137, "killed")):
            with self.subTest(status=status), AdmissionFixture() as fixture:
                request = fixture.request(need=12 * GIB, step="build")
                request = replace(request, argv=("cargo", "build", "--release", "--bin", "fixture"))
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                granted = only_admitted(fixture.server.handle_request(request))
                record = fixture.server.reservations[request.episode]
                fixture.scoped_peak(granted, 10 * GIB)
                release = wire.ReleaseRequest("once", granted.reservation, status)
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                row, = fixture.history()
                self.assertEqual((row["status"], row["outcome"], row["need"], row["reservation_id"]),
                                 (status, outcome, 12 * GIB, granted.reservation))
                for path in (record.path, granted.max_path, granted.wave_path, record.sidecar):
                    self.assertFalse(Path(path).exists(), path)
                self.assertEqual(row["shape"], build_shape(list(request.argv)).key)
                self.assertFalse(fixture.server.reservations)

    def test_two_release_builds_in_one_call_keep_shape_and_separate_history(self) -> None:
        with AdmissionFixture() as fixture:
            argv = ("cargo", "build", "--release", "--features=bevy/trace_chrome", "--bin", "hana")
            ids: list[str] = []
            for index in range(2):
                request = fixture.request(need=12 * GIB, step="build", call=test_phase.CallRecord("one-call"))
                request = replace(request, argv=argv, sidecar=str(fixture.scope(request.owner)))
                fixture.allow_next_start()
                grant = only_admitted(fixture.server.handle_request(request))
                ids.append(grant.reservation)
                record = fixture.server.reservations[request.episode]
                self.assertEqual(record.shape, build_shape(list(argv)).key)
                fixture.scoped_peak(grant, (10 + index) * GIB)
                release = wire.ReleaseRequest(f"release-{index}", grant.reservation, 0)
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            rows = fixture.history()
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["reservation_id"] for row in rows], ids)
            self.assertEqual([row["call_id"] for row in rows], ["one-call", "one-call"])
            self.assertEqual([row["anon_peak_bytes"] for row in rows], [10 * GIB, 11 * GIB])
            self.assertTrue(all(row["shape"] == build_shape(list(argv)).key for row in rows))

    def test_unfinished_history_append_retries_the_saved_outcome_once(self) -> None:
        from scripts.lint.test_memory_admit import stop_history_append, HISTORY_APPEND_STOPS
        for tail in HISTORY_APPEND_STOPS:
            with self.subTest(tail=tail), AdmissionFixture() as fixture:
                request = fixture.request(step="build")
                request = replace(request, sidecar=str(fixture.scope(request.owner)),
                    profile=replace(request.profile, worktree="build-Δ"))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(grant, 15 * GIB)
                release = wire.ReleaseRequest("stopped-release", grant.reservation, 137)
                with stop_history_append(store, tail):
                    with self.assertRaisesRegex(OSError, "injected stop"):
                        _ = fixture.server.handle_request(release)
                saved = service.read_state(fixture.state_dir).reservations[request.episode]
                self.assertIsInstance(saved.finalization, dict)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(replace(release, status=0)), [wire.Ok()])
                row, = fixture.history()
                self.assertEqual((row["outcome"], row["status"], row["anon_peak_bytes"]),
                                 ("killed", 137, 15 * GIB))
                self.assertEqual(row["worktree"], "build-Δ")
                self.assertFalse(fixture.server.reservations)

    def test_saved_finalization_keeps_scope_evidence_after_sidecar_removal(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 14 * GIB)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("stop", grant.reservation, 137))
            Path(request.sidecar).unlink()
            fixture.restart()
            row, = fixture.history()
            self.assertEqual((row["outcome"], row["anon_peak_bytes"]), ("killed", 14 * GIB))
            self.assertFalse(fixture.server.reservations)

    def test_peak_read_error_keeps_all_sampler_evidence_for_retry(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 14 * GIB)
            _ = Path(grant.wave_path).write_text(f"{14 * GIB} {fixture.clock.now()}")
            paths = (Path(grant.max_path), Path(grant.wave_path), Path(request.sidecar))
            before = {path: path.read_bytes() for path in paths}
            read = Path.read_text
            def fail_peak(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
                if path == Path(grant.max_path):
                    raise PermissionError("the peak is unreadable")
                return read(path, encoding=encoding, errors=errors)
            with mock.patch.object(Path, "read_text", fail_peak):
                with self.assertRaisesRegex(PermissionError, "peak is unreadable"):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("retry", grant.reservation, 0))
            self.assertEqual({path: path.read_bytes() for path in paths}, before)
            self.assertFalse(fixture.history())
            fixture.restart()
            row, = fixture.history()
            self.assertEqual(row["anon_peak_bytes"], 14 * GIB)
            self.assertFalse(fixture.server.reservations)

    def test_cleanup_retries_after_each_sampler_file_removal_without_duplicate_history(self) -> None:
        for suffix in (".max", ".wave", ".cgroup"):
            with self.subTest(suffix=suffix), AdmissionFixture() as fixture:
                request = fixture.request()
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(grant)
                stop = Path(request.sidecar) if suffix == ".cgroup" else Path(grant.max_path).with_suffix(suffix)
                unlink = Path.unlink
                def stop_after_removal(path: Path, missing_ok: bool = False) -> None:
                    unlink(path, missing_ok=missing_ok)
                    if path == stop:
                        raise InterruptedError("cleanup stopped")
                with mock.patch.object(Path, "unlink", stop_after_removal):
                    with self.assertRaisesRegex(InterruptedError, "cleanup stopped"):
                        _ = fixture.server.handle_request(wire.ReleaseRequest("cleanup", grant.reservation, 0))
                self.assertEqual(len(fixture.history()), 1)
                fixture.restart()
                self.assertEqual(len(fixture.history()), 1)
                self.assertFalse(fixture.server.reservations)
                for path in (Path(grant.max_path), Path(grant.wave_path), Path(request.sidecar)):
                    self.assertFalse(path.exists(), str(path))

    def test_unscoped_release_writes_no_peak_history(self) -> None:
        with AdmissionFixture() as fixture:
            grant = only_admitted(fixture.server.handle_request(fixture.request()))
            _ = Path(grant.max_path).write_text(str(14 * GIB))
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("unscoped", grant.reservation, 0)), [wire.Ok()])
            self.assertFalse(fixture.history())
            self.assertFalse(Path(grant.max_path).exists())


    def test_priority_override_and_frozen_release_keep_the_resolved_head(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            older = fixture.request(need=20 * GIB, since=fixture.clock.now() - 600)
            chosen = fixture.request(need=12 * GIB, since=fixture.clock.now() - 60)
            for request in (older, chosen):
                _ = fixture.server.handle_request(request)
            with mock.patch.object(gate, "priority_labels", return_value={
                    older.identity.session_id: UnitLabels("old", unit="other"),
                    chosen.identity.session_id: UnitLabels("chosen", unit="chosen")}):
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                    "choose", "chosen", "urgent", "director")), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(wire.AdmitHeadRequest(
                    "head", "checked free memory", "director")), [wire.Ok()])
                override = fixture.server.override
                assert not isinstance(override, records.NoHeadAdmissionOverride)
                self.assertEqual(override["pid"], chosen.owner.pid)
                explanation = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(explanation, wire.ExplainReply)
                self.assertEqual(explanation.explanation.ordered[0]["pid"], chosen.owner.pid)
                head = explanation.explanation.head
                assert isinstance(head, records.HeadExplanation)
                self.assertEqual(head.waiter.pid, chosen.owner.pid)
                frozen = explanation.explanation
                with mock.patch.object(gate, "priority_labels", side_effect=AssertionError("frozen roster")), \
                     mock.patch.object(Path, "read_text", side_effect=AssertionError("frozen files")):
                    self.assertIsInstance(gate.smallest_head_release(frozen), gate.HeadStillHeld)
                    self.assertIsInstance(gate.head_after_steps_end(frozen, ()), gate.HeadStillHeld)
                fixture.free(60 * GIB)
                _ = fixture.server.run_pass()
                self.assertIn(chosen.episode, fixture.server.reservations)
                self.assertIsInstance(fixture.server.override, records.NoHeadAdmissionOverride)

    def test_policy_edits_reclassify_pressure_in_both_directions(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="merge-gate", need=8 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 8 * GIB)
            pressure = Path(os.environ["BUILDLOG_MEMORY_PRESSURE"])
            fixture.free(0)
            fixture.hold_starts()
            _ = pressure.write_text("full avg10=7.0 avg60=0 avg300=0 total=0\nsome avg10=0\n")
            waiting = fixture.request()
            _ = fixture.server.handle_request(waiting)
            for old, new, held in ((5, 10, False), (10, 5, True)):
                fixture.configure(full_pressure_limit=old)
                _ = fixture.server.run_pass()
                before = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(before, wire.ExplainReply)
                fixture.configure(full_pressure_limit=new)
                _ = fixture.server.run_pass()
                after = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(after, wire.ExplainReply)
                self.assertEqual(isinstance(before.explanation.decided_from.pressure, records.MemoryPressure), not held)
                self.assertEqual(isinstance(after.explanation.decided_from.pressure, records.MemoryPressure), held)

    def test_ramp_growth_and_release_keep_peak_width_call_and_repository(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=12 * GIB, width=records.RequestedTestWidth(16), call=test_phase.CallRecord("call-width-16"))
            request = replace(request, sidecar=str(fixture.scope(request.owner, current=8 * GIB, anonymous=8 * GIB)),
                profile=records.StepProfile("hana", "nextest", "tree", records.ExpectedPeak(12 * GIB, "measured", 5),
                    records.RepositoryIdentity("https://example.org/hana", "hana"), "full", fixture.clock.now()))
            grant = only_admitted(fixture.server.handle_request(request))
            _ = fixture.server.run_pass()
            reading = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reading, wire.ViewReply)
            running = reading.reading.view.running[0].measured
            assert isinstance(running, records.MeasuredMemory)
            room = reading.reading.explanation.room
            assert isinstance(room, records.MemoryRoom)
            self.assertEqual((room.kept, room.used), (4 * GIB, 8 * GIB))
            fixture.scoped_peak(grant, 8 * GIB)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("end-ramp", grant.reservation, 7)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual((row["anon_peak_bytes"], row["status"], row["test_threads"], row["call_id"]),
                (8 * GIB, 7, 16, "call-width-16"))
            self.assertEqual((row["repo_identity"], row["test_scope"]), ("https://example.org/hana", "full"))
            self.assertFalse(Path(request.sidecar).exists())
            self.assertFalse(Path(grant.max_path).exists())
            self.assertFalse(fixture.server.reservations)


    def test_priority_list_appends_repeats_and_clears_units_without_changing_group_order(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            requests = [fixture.request(since=fixture.clock.now() - age) for age in (900, 600, 400, 200, 100)]
            units = ("other", "cables", "demo", "demo", "cables")
            labels = {request.identity.session_id: UnitLabels("worker", unit=unit)
                      for request, unit in zip(requests, units)}
            for request in requests:
                _ = fixture.server.handle_request(request)
            with mock.patch.object(gate, "priority_labels", return_value=labels), mock.patch.object(
                    buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged:
                for unit in ("demo", "cables"):
                    self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                        "bump-" + unit, unit, "why-" + unit, "actor-" + unit)), [wire.Ok()])
                saved = fixture.server.priority
                assert isinstance(saved, records.QueuePriority)
                self.assertEqual([entry["unit"] for entry in saved.entries], ["demo", "cables"])
                self.assertEqual([entry["why"] for entry in saved.entries], ["why-demo", "why-cables"])
                self.assertEqual([entry["set_by"] for entry in saved.entries], ["actor-demo", "actor-cables"])
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                    "repeat-demo", "demo", "replacement reason", "another actor")), [wire.Ok()])
                self.assertEqual(fixture.server.priority, saved)
                self.assertEqual(logged.call_count, 2)
                fixture.restart()
                for request in requests:
                    _ = fixture.server.handle_request(request)
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                    [requests[index].owner.pid for index in (2, 3, 1, 4, 0)])
                model = queue_view.QueueModel(view.reading, labels,
                    head_facts.read_head_facts(view.reading), head_facts.PaceUnavailable(), user_zone(),
                    queue_view.HostLoadUnavailable(), gate.smallest_head_release(view.reading.explanation))
                text = "queue priority: demo (why-demo), cables (why-cables)"
                self.assertIn(text, plain(queue_view.render_view(model, 400)))
                self.assertIn(text, plain(queue_card.render_selection(model, "1", 400)))
                facts = head_facts.read_head_facts(view.reading)
                assert isinstance(facts, head_facts.HeadFacts)
                self.assertIn(text, head_facts.order_text(facts))
                self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest(
                    "remove-demo", "director", "demo")), [wire.Ok()])
                priority = fixture.server.priority
                assert isinstance(priority, records.QueuePriority)
                self.assertEqual(priority.entries, saved.entries[1:])
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                    [requests[index].owner.pid for index in (1, 4, 0, 2, 3)])
                self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest(
                    "unknown-clear", "director", "missing")), [wire.Refused(
                        "unknown unit missing; the queue priority did not change")])
                self.assertEqual(fixture.server.priority, priority)
                self.assertEqual(logged.call_count, 3)
                self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest(
                    "clear-all", "director")), [wire.Ok()])
                self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
                self.assertEqual(logged.call_count, 4)
                self.assertEqual([call.args[:2] for call in logged.call_args_list],
                    [("set", "demo"), ("set", "cables"), ("clear", "demo"), ("clear", "cables")])

    def test_old_priority_file_and_service_state_read_as_one_entry(self) -> None:
        with AdmissionFixture() as fixture:
            old = {"unit": "demo", "why": "user work", "set_at": fixture.clock.now(),
                   "set_by": "director", "transition": {"kind": "SettledPriority"}}
            path = gate.queue_priority_path(fixture.state_dir)
            _ = path.write_text(json.dumps(old))
            before = path.read_bytes()
            priority = gate.load_queue_priority(fixture.state_dir)
            assert isinstance(priority, records.QueuePriority)
            self.assertEqual(priority.entries, ({"unit": "demo", "why": "user work",
                "set_at": fixture.clock.now(), "set_by": "director"},))
            self.assertEqual(path.read_bytes(), before)
            path = fixture.state_dir / "state.json"
            state = cast(dict[str, object], json.loads(path.read_text()))
            state["priority"] = old
            _ = path.write_text(json.dumps(state))
            before = path.read_bytes()
            restored = service.read_state(fixture.state_dir)
            self.assertEqual(restored.priority, priority)
            self.assertEqual(path.read_bytes(), before)
            fixture.restart()
            self.assertEqual(fixture.server.priority, priority)
            viewed, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(viewed, wire.ViewReply)
            self.assertEqual(viewed.reading.explanation.priority, priority)
            encoded = wire.encode_reply(viewed)
            self.assertEqual(wire.parse_reply(encoded), viewed)

    def test_priority_requires_a_roster_unit_and_replays_refusals(self) -> None:
        for running in (False, True):
            with self.subTest(running=running), AdmissionFixture() as fixture:
                request = fixture.request()
                fixture.free(60 * GIB if running else 0)
                _ = fixture.server.handle_request(request)
                absent = wire.PrioritySetRequest("absent", "missing", "urgent", "director")
                with mock.patch.object(gate, "priority_labels", return_value={
                        "unrelated": UnitLabels("other", unit="roster-unit"),
                        request.identity.session_id: UnitLabels("worker", unit="present")}):
                    refused = fixture.completed_queue_change(absent)
                    self.assertEqual(refused, [wire.Refused(
                        "unknown unit missing; the queue priority did not change")])
                    self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
                    self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                        "present", "present", "urgent", "director")), [wire.Ok()])
                    self.assertEqual(fixture.completed_queue_change(absent), refused)
                    priority = fixture.server.priority
                    assert not isinstance(priority, records.NoQueuePriority)
                    self.assertEqual(priority.entries[0]["unit"], "present")

    def test_waiter_keeps_source_basis_scope_and_typed_width_in_state_and_views(self) -> None:
        for source, runs in (("measured", 7), ("buildlog", 12), ("pooled", 31), ("fallback", 0)):
            with self.subTest(source=source), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request(width=records.RequestedTestWidth(8))
                assert isinstance(request.profile, records.StepProfile)
                request = replace(request, argv=("cargo", "nextest", "run", "-E",
                    "package(hana) & (test(one) | test(two))", "--test-threads", "8"),
                    profile=replace(request.profile, scope="filtered", repository=records.RepositoryIdentity(
                        "https://example.org/hana", "hana"), peak=records.ExpectedPeak(12 * GIB, source, runs)))
                _ = fixture.server.handle_request(request)
                row = fixture.server.waiters[request.episode].row
                self.assertEqual((row.get("need_source"), row.get("need_runs"), row["need"]), (source, runs, 12 * GIB))
                self.assertEqual((row.get("test_scope"), row.get("package"), row.get("filters")), ("filtered", "hana", ["one", "two"]))
                self.assertEqual(row.get("estimated_at"), fixture.clock.now())
                fixture.restart()
                _ = fixture.server.handle_request(request)
                saved = fixture.server.waiters[request.episode].row
                self.assertEqual(saved, row)
                reply = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(reply, wire.ExplainReply)
                head = reply.explanation.head
                assert isinstance(head, records.HeadExplanation)
                assert isinstance(head.need_source, records.RecordedNeed)
                self.assertEqual((head.need_source.source, head.need_source.runs, head.need_source.basis),
                    (source, runs, gate.read_need_basis(row.get("need_basis"))))
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request(step="clippy", width=records.NoTestWidth())
            _ = fixture.server.handle_request(request)
            stored_request = fixture.server.waiters[request.episode].request
            assert isinstance(stored_request, wire.AdmitRequest)
            self.assertIsInstance(stored_request.test_width, records.NoTestWidth)


    def test_priority_clear_and_replacement_replay_each_transition_once_after_a_crash(self) -> None:
        for action in ("clear", "replace"):
            with self.subTest(action=action), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request()
                _ = fixture.server.handle_request(request)
                _ = self.enterContext(mock.patch.object(gate, "priority_labels", return_value={
                    request.identity.session_id: UnitLabels("worker", unit="old")}))
                self.assertEqual(fixture.completed_queue_change(
                    wire.PrioritySetRequest("initial", "old", "initial", "director")), [wire.Ok()])
                mutation = (wire.PriorityClearRequest("change", "director") if action == "clear" else
                            wire.PrioritySetRequest("change", "new", "replacement", "director"))
                if action == "replace":
                    _ = self.enterContext(mock.patch.object(gate, "priority_labels", return_value={
                        request.identity.session_id: UnitLabels("worker", unit="new")}))
                with mock.patch.object(buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged:
                    fixture.fault.stage = "after_state_write"
                    with self.assertRaises(InjectedCrash):
                        _ = fixture.completed_queue_change(mutation)
                    logged.assert_not_called()
                    fixture.restart()
                    self.assertEqual(fixture.completed_queue_change(mutation), [wire.Ok()])
                    fixture.restart()
                    self.assertEqual(fixture.completed_queue_change(mutation), [wire.Ok()])
                logged.assert_called_once()
                self.assertEqual(logged.call_args.kwargs["transition_id"], mutation.request_id)
                if action == "clear":
                    self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
                else:
                    priority = fixture.server.priority
                    assert not isinstance(priority, records.NoQueuePriority)
                    self.assertEqual([entry["unit"] for entry in priority.entries], ["old", "new"])

    def test_unowned_scope_memory_refreshes_without_a_waiter_or_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "builds.slice/unowned.scope"
            scope.mkdir(parents=True)
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "memory.current").write_text(str(GIB))
            _ = (scope / "memory.stat").write_text("inactive_file 0\n")
            _ = fixture.server.run_pass()
            first = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(first, wire.ViewReply)
            measured = first.reading.view.running[0].measured
            assert isinstance(measured, records.UnownedSliceMemory)
            self.assertEqual(measured.bytes, GIB)
            _ = (scope / "memory.current").write_text(str(17 * GIB))
            _ = (scope / "memory.stat").write_text("inactive_file 0\n")
            fixture.clock.advance(records.read_memory_config().running_pass_seconds)
            fixture.server.run_once(0)
            updated = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(updated, wire.ViewReply)
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)
            measured = updated.reading.view.running[0].measured
            assert isinstance(measured, records.UnownedSliceMemory)
            self.assertEqual(measured.bytes, 17 * GIB)

    def test_finalization_crash_after_row_removal_expires_the_orphan_replay(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant)
            def crash_after_removal(stage: str) -> None:
                if stage != "after_state_write":
                    return
                saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                if not saved.reservations and request.episode in saved.ledger:
                    raise InjectedCrash("reservation removed before replay end")
            with mock.patch.object(fixture.server, "fault", side_effect=crash_after_removal):
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
            self.assertEqual(len(fixture.history()), 1)
            fixture.restart()
            self.assertFalse(fixture.server.reservations)
            fixture.clock.advance(2 * records.read_memory_config().replay_retention_seconds)
            fixture.server.persist()
            self.assertNotIn(request.episode, fixture.server.ledger)
            fixture.restart()
            self.assertNotIn(request.episode, fixture.server.ledger)
            self.assertEqual(len(fixture.history()), 1)


    def test_priority_replay_after_a_crash_keeps_one_transition_and_the_same_order(self) -> None:
        from scripts.message.unit_labels import UnitLabels
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first, second = fixture.request(), fixture.request()
            labels = {first.identity.session_id: UnitLabels("first", unit="other"),
                      second.identity.session_id: UnitLabels("second", unit="queue")}
            with mock.patch.object(gate, "priority_labels", return_value=labels), \
                 mock.patch.object(buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged:
                _ = fixture.server.handle_request(first)
                _ = fixture.server.handle_request(second)
                mutation = wire.PrioritySetRequest("priority-once", "queue", "urgent", "director")
                fixture.fault.stage = "after_state_write"
                with self.assertRaises(InjectedCrash):
                    _ = fixture.completed_queue_change(mutation)
                logged.assert_not_called()
                fixture.restart()
                self.assertEqual(fixture.completed_queue_change(mutation), [wire.Ok()])
                for request in (first, second):
                    _ = fixture.server.handle_request(request)
                fixture.restart()
                for request in (first, second):
                    _ = fixture.server.handle_request(request)
                self.assertEqual(fixture.completed_queue_change(mutation), [wire.Ok()])
            logged.assert_called_once()
            self.assertEqual(logged.call_args.kwargs["transition_id"], mutation.request_id)
            viewed = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(viewed, wire.ViewReply)
            self.assertEqual([row["pid"] for _, row in viewed.reading.ordered],
                             [second.owner.pid, first.owner.pid])
            self.assertEqual([records.waiter_priority(row) for _, row in viewed.reading.ordered],
                             [records.PriorityUnit("queue"), records.NormalOrder()])
            priority = viewed.reading.explanation.priority
            assert not isinstance(priority, records.NoQueuePriority)
            self.assertEqual(priority.entries[0]["unit"], "queue")

    def test_token_replay_after_a_crash_restores_the_place_and_records_one_gap(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request()
            first = replace(first, sidecar=str(fixture.scope(first.owner)))
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.scoped_peak(grant)
            before, = fixture.server.places.values()
            mutation = wire.TokenWaitRequest("token-once", grant.reservation)
            fixture.fault.stage = "after_state_write"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(mutation)
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
            self.assertFalse(fixture.server.reservations)
            place, = fixture.server.places.values()
            self.assertEqual((place.owner, place.since, place.priority_since),
                             (before.owner, before.since, before.priority_since))
            self.assertIsInstance(place.token_left_at, float)
            self.assertEqual([row["reservation_id"] for row in fixture.history()], [grant.reservation])
            fixture.clock.advance(1)
            fixture.free(0)
            fixture.hold_starts()
            second = replace(first, episode="after-token", token_back_at=fixture.clock.now())
            _ = fixture.server.handle_request(second)
            _ = fixture.server.handle_request(second)
            row = fixture.server.waiters[second.episode].row
            self.assertEqual(row["since"], first.since)
            self.assertEqual(row.get("token_left_at"), place.token_left_at)
            fixture.restart()
            _ = fixture.server.handle_request(second)
            with (fixture.state_dir / "queue_times.jsonl").open("rb") as stream:
                times = list(gate.queue_times_backward(stream))
            gaps = [row for row in times if row.get("kind") == "token_gap"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["episode"], second.episode)
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            _ = fixture.server.run_pass()
            later = fixture.server.ledger[second.episode].reply
            assert isinstance(later, wire.Admitted)
            self.assertNotEqual(later.reservation, grant.reservation)

    def test_new_failed_build_peak_expires_matching_waiters_without_losing_their_place(self) -> None:
        with AdmissionFixture() as fixture:
            argv = ("cargo", "build", "--release", "--bin", "fixture")
            repository = records.RepositoryIdentity("https://example.org/fixture", "fixture")
            shape = build_shape(list(argv))
            profile = records.StepProfile("fixture", "build", str(fixture.root),
                records.ExpectedPeak(12 * GIB, "shape", 9,
                    records.ShapeRankedPeak(9, 10, records.NoFloor(), records.ReleaseHeadroom(records.read_memory_config(report=False).release_headroom), records.UnderBudget())),
                repository, "full", fixture.clock.now())
            owner_request = fixture.request(need=12 * GIB, step="build")
            assert isinstance(owner_request.profile, records.StepProfile)
            profile = replace(profile, build_history_generation=owner_request.profile.build_history_generation)
            owner_request = replace(owner_request, argv=argv, profile=profile,
                sidecar=str(fixture.scope(owner_request.owner)))
            grant = only_admitted(fixture.server.handle_request(owner_request))
            peak = int(15.76 * GIB)
            fixture.scoped_peak(grant, peak)
            fixture.free(0)
            fixture.hold_starts()
            waiter = replace(fixture.request(need=12 * GIB, step="build",
                since=fixture.clock.now() - 900), argv=argv, profile=profile)
            _ = fixture.server.handle_request(waiter)
            before = dict(fixture.server.waiters[waiter.episode].row)
            fixture.proc.kill(owner_request.owner)
            scope = Path(Path(owner_request.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            _ = fixture.server.run_pass()
            row, = fixture.history()
            self.assertEqual((row["outcome"], row["need"], row["shape"], row["anon_peak_bytes"]),
                ("owner_gone", 12 * GIB, shape.key, peak))
            waiting = fixture.server.waiters[waiter.episode]
            held = waiting.last_reply
            assert isinstance(held, wire.Wait)
            self.assertIsInstance((held.reason.cause if isinstance(held.reason, records.HeadAdmissionStatus) else held.reason), records.EstimateExpired)
            for key in ("since", "priority_since", "need", "need_basis"):
                self.assertEqual(waiting.row.get(key), before.get(key))
            self.assertNotIn(owner_request.episode, fixture.server.reservations)
            self.assertNotIn(waiter.episode, fixture.server.reservations)
            fixture.clock.advance(1)
            fixture.free(60 * GIB)
            blocker = fixture.request(need=GIB // 8, step="check",
                owner=fixture.proc.spawn(used=GIB // 8))
            if blocker.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            blocker_grant = only_admitted(fixture.server.handle_request(blocker))
            fixture.free(18 * GIB)
            with test_hang_guard.LauncherHangGuard(self.id()).protect(), fixture.serving(), \
                    mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(step_estimator, "git_identity", return_value=(repository, str(fixture.root))):
                refreshed = client.step_request(list(argv), str(fixture.root), waiter.owner,
                    waiter.since, waiter.sidecar, waiter.episode)
            self.assertEqual((refreshed.owner, refreshed.episode, refreshed.since),
                (waiter.owner, waiter.episode, waiter.since))
            self.assertIsInstance(refreshed.profile, records.StepProfile)
            assert isinstance(refreshed.profile, records.StepProfile)
            self.assertEqual(refreshed.profile.build_history_generation,
                records.BuildHistoryGeneration(fixture.server.build_history_generation))
            self.assertGreater(refreshed.profile.peak.bytes, peak)
            self.assertEqual(fixture.server.reservations[blocker.episode].reservation_id,
                blocker_grant.reservation)
            next_attempt = replace(waiter, profile=refreshed.profile)
            reply, = fixture.server.handle_request(next_attempt)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertNotIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
            self.assertGreater(reply.snapshot.peak.bytes, 15.76 * GIB)
            self.assertEqual(reply.snapshot.decision.state, "hold")
            self.assertEqual(reply.snapshot.peak, next_attempt.profile.peak)
            self.assertNotIn(waiter.episode, fixture.server.reservations)
            fixture.free(60 * GIB)
            if next_attempt.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            admitted = only_admitted(fixture.server.handle_request(next_attempt))
            self.assertEqual(admitted.need, next_attempt.profile.peak.bytes)
            self.assertEqual(fixture.server.reservations[waiter.episode].need, next_attempt.profile.peak.bytes)

    def build_history_request(self, fixture: AdmissionFixture, *, since: float = 900) -> wire.AdmitRequest:
        request = fixture.request(need=12 * GIB, step="build", since=since)
        assert isinstance(request.profile, records.StepProfile)
        profile = replace(request.profile, worktree=str(fixture.root),
            repository=records.RepositoryIdentity("https://example.org/fixture", "fixture"),
            peak=records.ExpectedPeak(12 * GIB, "shape", 9,
                records.ShapeRankedPeak(9, 10, records.NoFloor(), records.ReleaseHeadroom(records.read_memory_config(report=False).release_headroom), records.UnderBudget())))
        return replace(request, argv=("cargo", "build", "--release", "--bin", "fixture"), profile=profile)

    def assert_unregistered_build_is_expired(self, *, restart: bool = False,
            corrected_clock: bool = False, longer_head: bool = False) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = 1000
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.clock.epoch = 1050
            stale = self.build_history_request(fixture)
            self.assertNotIn(stale.episode, fixture.server.waiters)
            self.assertFalse(any(place.owner == stale.owner for place in fixture.server.places.values()))
            assert isinstance(stale.profile, records.StepProfile)
            generation = stale.profile.build_history_generation
            assert isinstance(generation, records.BuildHistoryGeneration)
            fixture.clock.epoch = 1040 if corrected_clock else 1100
            _ = fixture.server.handle_request(wire.ReleaseRequest("published-before-connect", grant.reservation, 137))
            fixture.clock.advance(0.001)
            fixture.server.persist()
            if restart:
                fixture.restart()
            if longer_head:
                old_head = records.read_memory_config().policy.timing.estimate_refresh_seconds
                fixture.clock.epoch = stale.profile.estimated_at + old_head + 2
                fixture.configure(estimate_refresh_seconds=old_head * 2)
                self.assertLess(fixture.clock.now() - stale.profile.estimated_at,
                    records.read_memory_config().policy.timing.estimate_refresh_seconds)
            if corrected_clock:
                self.assertLess(fixture.clock.now(), stale.profile.estimated_at)
            history, = fixture.history()
            self.assertEqual(history["anon_peak_bytes"], 16 * GIB)
            self.assertGreater(service.read_state(fixture.state_dir).build_history_generation, generation.value)
            self.assertFalse(fixture.server.waiters)
            fixture.free(18 * GIB)
            reply, = fixture.server.handle_request(stale)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
            self.assertNotIn(stale.episode, fixture.server.reservations)
            self.assertEqual(fixture.server.waiters[stale.episode].row["since"], stale.since)
            self.assertLess(stale.profile.peak.bytes + reply.snapshot.decision.reserve, 18 * GIB)

    def test_unregistered_build_estimate_expires_after_publication_and_persistence(self) -> None:
        self.assert_unregistered_build_is_expired()

    def test_unregistered_build_estimate_expires_after_publication_and_restart(self) -> None:
        self.assert_unregistered_build_is_expired(restart=True)

    def test_clock_correction_cannot_revive_a_build_estimate_after_publication(self) -> None:
        self.assert_unregistered_build_is_expired(corrected_clock=True)

    def test_larger_head_seconds_cannot_revive_a_build_estimate_after_publication(self) -> None:
        self.assert_unregistered_build_is_expired(longer_head=True)

    def test_build_admit_and_launch_require_a_current_history_generation(self) -> None:
        for launch in (False, True):
            for missing in (False, True):
                with self.subTest(launch=launch, missing=missing), AdmissionFixture() as fixture:
                    stale = self.build_history_request(fixture)
                    assert isinstance(stale.profile, records.StepProfile)
                    if missing:
                        profile = replace(stale.profile, build_history_generation=records.NoBuildHistoryGeneration())
                        stale = replace(stale, profile=profile)
                    else:
                        owner = self.build_history_request(fixture)
                        owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
                        grant = only_admitted(fixture.server.handle_request(owner))
                        fixture.scoped_peak(grant, 16 * GIB)
                        _ = fixture.server.handle_request(wire.ReleaseRequest("advance-before-launch", grant.reservation, 137))
                    fixture.free(18 * GIB)
                    request: wire.AdmitRequest | wire.LaunchRequest = stale
                    if launch:
                        request = wire.LaunchRequest(stale.episode, stale.owner, stale.since, stale.identity, stale.profile)
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.Wait)
                    assert isinstance(reply, wire.Wait)
                    self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
                    self.assertNotIn(stale.episode, fixture.server.reservations)
                    self.assertNotIn(stale.episode, fixture.server.ledger)

    def test_current_generation_refresh_preserves_owner_episode_and_queue_place(self) -> None:
        with AdmissionFixture() as fixture:
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            stale = self.build_history_request(fixture)
            _ = fixture.server.handle_request(wire.ReleaseRequest("peak-before-refresh", grant.reservation, 137))
            fixture.free(18 * GIB)
            reply, = fixture.server.handle_request(stale)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
            before = dict(fixture.server.waiters[stale.episode].row)
            with test_hang_guard.LauncherHangGuard(self.id()).protect(), fixture.serving(), \
                    mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(step_estimator, "git_identity", return_value=(stale.profile.repository, str(fixture.root))):
                refreshed = client.step_request(list(stale.argv), stale.cwd, stale.owner,
                    stale.since, stale.sidecar, stale.episode)
            self.assertEqual((refreshed.owner, refreshed.episode, refreshed.since),
                (stale.owner, stale.episode, stale.since))
            self.assertGreater(refreshed.profile.peak.bytes, stale.profile.peak.bytes)
            fixture.free(60 * GIB)
            if refreshed.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            granted = only_admitted(fixture.server.handle_request(refreshed))
            self.assertEqual(granted.need, refreshed.profile.peak.bytes)
            record = fixture.server.reservations[stale.episode]
            self.assertEqual(record.owner, stale.owner)
            for key in ("since", "priority_since"):
                self.assertEqual(record.queue_waiter.get(key), before.get(key))

    def test_retry_of_an_existing_history_row_advances_the_generation(self) -> None:
        with AdmissionFixture() as fixture:
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            release = wire.ReleaseRequest("retry-published-row", grant.reservation, 137)
            with mock.patch.object(service.StateIntents, "published", side_effect=OSError("generation save failed")):
                with self.assertRaisesRegex(OSError, "generation save failed"):
                    _ = fixture.server.handle_request(release)
            history, = fixture.history()
            self.assertEqual(history["reservation_id"], grant.reservation)
            stale = self.build_history_request(fixture)
            before = service.read_state(fixture.state_dir).build_history_generation
            fixture.restart()
            self.assertGreater(service.read_state(fixture.state_dir).build_history_generation, before)
            self.assertEqual(len(fixture.history()), 1)
            fixture.free(18 * GIB)
            reply, = fixture.server.handle_request(stale)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
            self.assertNotIn(stale.episode, fixture.server.reservations)

    def test_delayed_build_refresh_cannot_clear_a_later_failed_peak(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = 1000
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            profile = owner.profile
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.free(0)
            fixture.hold_starts()
            age = records.read_memory_config().policy.timing.estimate_refresh_seconds + 1
            waiter = replace(self.build_history_request(fixture),
                profile=replace(profile, estimated_at=1000 - age))
            first, = fixture.server.handle_request(waiter)
            assert isinstance(first, wire.Wait)
            self.assertIsInstance((first.reason.cause if isinstance(first.reason, records.HeadAdmissionStatus) else first.reason), records.EstimateExpired)
            before = dict(fixture.server.waiters[waiter.episode].row)
            delayed = replace(waiter, profile=replace(profile, estimated_at=1005))
            fixture.clock.epoch = 1010
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                "finished-after-refresh-read", grant.reservation, 137)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual(row["anon_peak_bytes"], 16 * GIB)
            fixture.free(18 * GIB)
            fixture.clock.epoch = 1011
            reply, = fixture.server.handle_request(delayed)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
            self.assertNotIn(waiter.episode, fixture.server.reservations)
            self.assertEqual(fixture.server.waiters[waiter.episode].row["since"], before["since"])
            self.assertLess(12 * GIB + reply.snapshot.decision.reserve, 18 * GIB)

    def test_pending_failed_peak_expires_estimates_before_and_after_restart_publication(self) -> None:
        for append_fails in (False, True):
            with self.subTest(append_fails=append_fails), AdmissionFixture() as fixture:
                fixture.clock.epoch = 1000
                owner = self.build_history_request(fixture)
                owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
                grant = only_admitted(fixture.server.handle_request(owner))
                fixture.scoped_peak(grant, 16 * GIB)
                fixture.clock.epoch = 1010
                fixture.fault.stage = "after_finalization_intent"
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("pending-failed-peak", grant.reservation, 137))
                saved = service.read_state(fixture.state_dir).reservations[owner.episode]
                intent = saved.finalization
                assert not isinstance(intent, service.NoFinalizationIntent)
                self.assertEqual(datetime.fromisoformat(intent["ended_at"]).timestamp(), 1010)
                self.assertEqual(fixture.history(), [])

                fixture.clock.epoch = 1050
                stale = self.build_history_request(fixture)
                assert isinstance(stale.profile, records.StepProfile)
                self.assertEqual(stale.profile.build_history_generation, fixture.build_history_generation())
                fixture.free(18 * GIB)
                with self.subTest(boundary="pending finalization"), \
                        mock.patch.object(store, "append_locked", side_effect=OSError("history unavailable")):
                    reply, = fixture.server.handle_request(stale)
                    self.assertIsInstance(reply, wire.Wait)
                    assert isinstance(reply, wire.Wait)
                    self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
                    self.assertNotIn(stale.episode, fixture.server.reservations,
                        "a pending failed peak must prevent admission with the stale need")
                    self.assertEqual(fixture.server.waiters[stale.episode].row["since"], stale.since)
                self.assertEqual(fixture.history(), [])

                if append_fails:
                    fixture.clock.epoch = 1075
                    self.assertEqual(fixture.proc.owner_start(owner.owner.pid),
                        service.LiveOwnerIdentity(owner.owner.start_time))
                    with mock.patch.object(store, "append_locked",
                            side_effect=OSError("history append failed")) as append:
                        fixture.restart()
                        with self.subTest(boundary="failed append after restart"):
                            reply, = fixture.server.handle_request(stale)
                            self.assertIsInstance(reply, wire.Wait)
                            assert isinstance(reply, wire.Wait)
                            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
                            self.assertNotIn(stale.episode, fixture.server.reservations,
                                "a failed history append must prevent stale admission")
                            retained = service.read_state(fixture.state_dir).reservations[owner.episode]
                            self.assertEqual(retained.finalization, intent)
                            self.assertEqual(fixture.history(), [])
                            self.assertGreaterEqual(append.call_count, 2,
                                "restart and admission must retry the pending append")

                fixture.clock.epoch = 1100
                if append_fails:
                    _ = fixture.server.run_pass()
                    self.assertEqual(fixture.proc.owner_start(owner.owner.pid),
                        service.LiveOwnerIdentity(owner.owner.start_time))
                else:
                    fixture.restart()
                history, = fixture.history()
                self.assertEqual((history["reservation_id"], history["anon_peak_bytes"]),
                    (grant.reservation, 16 * GIB))
                self.assertNotIn(owner.episode, fixture.server.reservations)
                with self.subTest(boundary="history published after restart"):
                    reply, = fixture.server.handle_request(stale)
                    self.assertIsInstance(reply, wire.Wait)
                    assert isinstance(reply, wire.Wait)
                    self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
                    self.assertNotIn(stale.episode, fixture.server.reservations,
                        "publication must invalidate estimates made after the intent and before the append")
                    self.assertEqual(fixture.server.waiters[stale.episode].row["since"], stale.since)
                    self.assertLess(stale.profile.peak.bytes + reply.snapshot.decision.reserve, 18 * GIB)

    def test_rollback_export_finalizes_an_ended_reservation_before_removing_state(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("unfinished-export", grant.reservation, 137))
            fixture.proc.kill(request.owner)
            scope = Path(Path(request.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            fixture.server.close()
            self.assertEqual(fixture.history(), [])
            finalize = gate.finalize_reservation
            def record_before_retirement(path: Path, row: records.Reservation, outcome: step_estimator.FinalOutcome,
                    now: datetime, status: int | step_estimator.OutcomeNotRecorded = gate.OUTCOME_NOT_RECORDED, *,
                    intents: gate.FinalizationIntents, end_kind: gate.StepEnd | gate.TokenReturn
                    ) -> gate.HistoryRecorded | gate.NoHistoryRecorded:
                self.assertTrue((fixture.state_dir / "state.json").exists())
                return finalize(path, row, outcome, now, status, intents=intents, end_kind=end_kind)
            with mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(gate, "finalize_reservation", side_effect=record_before_retirement) as finished:
                service.export_priority(fixture.state_dir)
            self.assertEqual(finished.call_count, 1)
            row, = fixture.history()
            self.assertEqual((row["reservation_id"], row["anon_peak_bytes"], row["status"]),
                (grant.reservation, 16 * GIB, 137))
            self.assertFalse((fixture.state_dir / "state.json").exists())

    def test_rollback_export_keeps_state_when_pending_finalization_fails(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("failed-export", grant.reservation, 137))
            fixture.proc.kill(request.owner)
            scope = Path(Path(request.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            fixture.server.close()
            with mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(gate, "finalize_reservation", side_effect=OSError("history append failed")):
                with self.assertRaisesRegex(ValueError, grant.reservation):
                    service.export_priority(fixture.state_dir)
            saved = service.read_state(fixture.state_dir)
            self.assertIn(request.episode, saved.reservations)
            self.assertIsInstance(saved.reservations[request.episode].finalization, dict)
            self.assertEqual(fixture.history(), [])


    def test_service_drain_ignores_unknown_state_fields(self) -> None:
        from scripts.build_queue.admission_test_support import TestProcesses
        state = service.ServiceState({}, {}, {}, records.ReadyToStart(), records.NoHeadAdmissionOverride())
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            proc = TestProcesses(directory / "proc")
            record = wire.parse_record(wire.encode_record(state), dict[str, object])
            record["unknown"] = True
            _ = (directory / "state.json").write_text(wire.encode_record(record))
            self.assertEqual(service.drain_check(
                directory, service=True, include_gate_callers=False, proc=proc), 0)
            self.assertEqual(service.read_state(directory), state)

    def service_drain_with_process(self, script: str, *, default_folder: bool) -> tuple[int, str]:
        from scripts.build_queue.admission_test_support import TestProcesses
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "admission"
            directory.mkdir()
            state = service.ServiceState({}, {}, {}, records.ReadyToStart(), records.NoHeadAdmissionOverride())
            _ = (directory / "state.json").write_text(wire.encode_record(state))
            proc = TestProcesses(root / "proc")
            try:
                owner = proc.spawn()
                _ = (proc.path / str(owner.pid) / "cmdline").write_bytes(
                    f"python3\0/private/{script}\0".encode())
                argv = ["admission_service", "drain-check", "--service", "--state-dir", str(directory)]
                output = io.StringIO()
                with mock.patch.object(store, "root", return_value=root if default_folder else root / "other"), \
                     mock.patch.object(service, "SystemProc", return_value=proc), \
                     mock.patch.object(sys, "argv", argv), mock.patch("sys.stdout", output):
                    status = service.main()
                return status, output.getvalue()
            finally:
                proc.close()

    def test_serve_without_state_dir_uses_the_buildlog_admission_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = mock.MagicMock()
            with mock.patch.object(store, "root", return_value=root), \
                 mock.patch.object(service, "AdmissionServer", return_value=server) as made, \
                 mock.patch.object(sys, "argv", ["admission_service", "serve"]):
                self.assertEqual(service.main(), 0)
            self.assertEqual(made.call_args.args[0], root / "admission")

    def test_service_drain_other_folder_ignores_verify_process(self) -> None:
        status, output = self.service_drain_with_process("verify.sh", default_folder=False)
        self.assertEqual(status, 0)
        self.assertEqual(output, "drained\n")

    def test_service_drain_other_folder_counts_admission_client(self) -> None:
        status, output = self.service_drain_with_process("admission_client.py", default_folder=False)
        self.assertEqual(status, 1)
        self.assertIn("clients: 1:", output)
        self.assertIn("waiters: 0\nreservations: 0\n", output)

    def test_service_drain_default_folder_counts_verify_process(self) -> None:
        status, output = self.service_drain_with_process("verify.sh", default_folder=True)
        self.assertEqual(status, 1)
        self.assertIn("clients: 1:", output)
        self.assertIn("waiters: 0\nreservations: 0\n", output)

    def test_drain_barriers_count_live_waiters_reservations_and_client_processes(self) -> None:
        import json
        for rollback in (False, True):
            for kind in ("waiter", "reservation", "client", "empty"):
                with self.subTest(rollback=rollback, kind=kind), AdmissionFixture() as fixture:
                    request = fixture.request()
                    if kind == "waiter":
                        if rollback:
                            fixture.free(0)
                            fixture.hold_starts()
                            _ = fixture.server.handle_request(request)
                        else:
                            _ = (fixture.state_dir / "old.waiting").write_text(json.dumps({
                                "pid": request.owner.pid, "start_time": request.owner.start_time,
                                "need": GIB, "since": fixture.clock.now()}))
                    elif kind == "reservation":
                        if rollback:
                            _ = fixture.server.handle_request(request)
                        else:
                            _ = (fixture.state_dir / "old.reservation").write_text(json.dumps({
                                "pid": request.owner.pid, "start_time": request.owner.start_time,
                                "need": GIB, "repo": "fixture", "step": "check", "worktree": "tree",
                                "admitted_at": store.utc_iso(fixture.clock.now()),
                                "sidecar": "", "test_threads": None, "call_id": None}))
                    elif kind == "client":
                        script = "admission_client.py" if rollback else "verify.sh"
                        _ = (fixture.proc.path / str(request.owner.pid) / "cmdline").write_bytes(
                            f"python3\0/private/{script}\0".encode())
                    before = fixture.state_text()
                    output = io.StringIO()
                    with mock.patch("sys.stdout", output):
                        status = service.drain_check(fixture.state_dir, service=rollback,
                                                     include_gate_callers=True, proc=fixture.proc)
                    self.assertEqual(status, 0 if kind == "empty" else 1)
                    if kind == "empty":
                        self.assertEqual(output.getvalue(), "drained\n")
                    else:
                        label = {"waiter": "waiters", "reservation": "reservations", "client": "clients"}[kind]
                        self.assertIn(f"{label}: 1", output.getvalue())
                    self.assertEqual(fixture.state_text(), before)


    def phase_grant(self, fixture: AdmissionFixture
                    ) -> tuple[wire.AdmitRequest, wire.Admitted, Path, records.TestPhaseNeed]:
        need = records.TestPhaseNeed(2 * GIB, 10, 8, 8, "same_width", 1.2)
        request = fixture.request(need=11 * GIB)
        request = replace(request, sidecar=str(fixture.scope(request.owner)))
        grant = only_admitted(fixture.server.handle_request(request))
        record = replace(fixture.server.reservations[request.episode], test_phase_need=need)
        fixture.server.reservations[request.episode] = record
        fixture.server.persist()
        fixture.scoped_peak(grant, 11 * GIB)
        phase = gate.test_phase_path(service.reservation_row(record))
        assert isinstance(phase, Path)
        fields: dict[str, object] = {"test_phase_peak_bytes": 3 * GIB,
            "test_phase_started_at": fixture.clock.now() - 30, "test_threads": 8,
            "call_id": "call-1", "test_scope": "full", "repo_identity": "fixture",
            "package": "", "filters": []}
        _ = phase.write_text(wire.encode_record(fields))
        return request, grant, phase, need


    def test_saved_test_phase_margin_and_evidence_survive_finalization_crashes(self) -> None:
        for stage in ("after_finalization_intent", "after_history_append"):
            with self.subTest(stage=stage), AdmissionFixture() as fixture:
                request, grant, phase, need = self.phase_grant(fixture)
                _ = fixture.server.run_pass()
                evidence: test_phase.TestPhaseChargeUse = {"bytes": 2 * GIB,
                    "selected_at": fixture.clock.now(), "reservation_id": grant.reservation,
                    "need": gate.test_phase_need_record(need)}
                fixture.server.reservations[request.episode] = replace(
                    fixture.server.reservations[request.episode], charge_use=evidence)
                fixture.server.persist()
                write_memory_config(fixture.root / "build_admission.conf", estimate_refresh_seconds="900")
                inputs = fixture.server.last_inputs
                assert isinstance(inputs, records.QueueInputs)
                self.assertEqual(len(inputs.running), 2)
                memory, = (step for step in inputs.running if gate.running_has_reservation(step))
                self.assertEqual(memory.promise, 11 * GIB)
                fixture.fault.stage = stage
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 137))
                phase.unlink()
                fixture.restart()
                _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
                row, = fixture.history()
                self.assertEqual((row["outcome"], row["test_phase_peak_bytes"]), ("killed", 3 * GIB))
                self.assertEqual(gate.read_test_phase_need(row["test_phase_need"]), need)
                self.assertEqual(row["test_phase_charge_use"], evidence)
                self.assertFalse(fixture.server.reservations)

    def test_ramp_owner_exit_records_the_peak_without_inventing_charge_evidence(self) -> None:
        with AdmissionFixture() as fixture:
            request, grant, phase, need = self.phase_grant(fixture)
            _ = fixture.server.run_pass()
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(len(inputs.running), 2)
            memory, = (step for step in inputs.running if gate.running_has_reservation(step))
            self.assertEqual(memory.promise, 11 * GIB)
            self.assertIsInstance(fixture.server.reservations[request.episode].charge_use, test_phase.NoTestPhaseChargeUse)
            fixture.proc.kill(request.owner)
            record = fixture.server.reservations[request.episode]
            scope = Path(Path(record.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            row, = fixture.history()
            self.assertEqual((row["outcome"], row["test_phase_peak_bytes"]), ("owner_gone", 3 * GIB))
            self.assertEqual(gate.read_test_phase_need(row["test_phase_need"]), need)
            self.assertNotIn("test_phase_charge_use", row)
            self.assertTrue(phase.exists())
            self.assertFalse(fixture.server.reservations)
            self.assertFalse(Path(grant.max_path).exists())

    def test_old_sampler_history_keeps_the_need_without_test_phase_window_fields(self) -> None:
        with AdmissionFixture() as fixture:
            request, grant, phase, need = self.phase_grant(fixture)
            phase.unlink()
            _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
            row, = fixture.history()
            self.assertEqual(gate.read_test_phase_need(row["test_phase_need"]), need)
            for field in ("test_phase_peak_bytes", "test_phase_started_at", "test_phase_charge_use"):
                self.assertNotIn(field, row)
            self.assertNotIn(request.episode, fixture.server.reservations)


    def test_priority_orders_new_workers_and_drops_rank_from_persisted_places(self) -> None:
        from scripts.message.unit_labels import UnitLabels
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            older = fixture.request(need=12 * GIB, since=fixture.clock.now() - 600)
            chosen = fixture.request(need=12 * GIB, since=fixture.clock.now() - 60)
            newest = fixture.request(need=12 * GIB, since=fixture.clock.now() - 30)
            labels = {older.identity.session_id: UnitLabels("older", unit="other"),
                chosen.identity.session_id: UnitLabels("chosen", unit="chosen"),
                newest.identity.session_id: UnitLabels("copy", unit="chosen")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                for request in (older, chosen):
                    _ = fixture.server.handle_request(request)
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest("priority", "chosen", "user work", "director")), [wire.Ok()])
                _ = fixture.server.handle_request(newest)
                view = fixture.server.handle_request(wire.ViewRequest())[0]
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                    [chosen.owner.pid, newest.owner.pid, older.owner.pid])
                state = service.read_state(fixture.state_dir)
                before = {key: (place.since, place.priority_since)
                    for key, place in state.places.items()}
                for waiting in fixture.server.waiters.values():
                    self.assertNotIn("priority_rank", records.stored_waiter(waiting.row))
                self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest("clear", "director")), [wire.Ok()])
                view = fixture.server.handle_request(wire.ViewRequest())[0]
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                    [older.owner.pid, chosen.owner.pid, newest.owner.pid])
                self.assertEqual({key: (place.since, place.priority_since)
                    for key, place in fixture.server.places.items()}, before)


    def test_estimate_expiry_keeps_recovery_place_and_basis_until_refresh(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            policy = fixture.server.last_policy
            assert isinstance(policy, records.AdmissionPolicy)
            request = fixture.request(age=policy.timing.estimate_refresh_seconds - 1)
            _ = fixture.server.handle_request(request)
            place = dict(fixture.server.waiters[request.episode].row)
            fixture.server.spacing = records.NextStartAt(fixture.clock.now() + 30)
            recovery = fixture.server.spacing
            fixture.clock.advance(2)
            _ = fixture.server.run_pass()
            held = fixture.server.waiters[request.episode].last_reply
            assert isinstance(held, wire.Wait)
            self.assertIsInstance((held.reason.cause if isinstance(held.reason, records.HeadAdmissionStatus) else held.reason), records.EstimateExpired)
            saved = fixture.server.waiters[request.episode].row
            for key in ("since", "priority_since", "need", "estimated_at", "need_source", "need_basis"):
                self.assertEqual(saved.get(key), place.get(key))
            self.assertEqual(fixture.server.spacing, recovery)
            self.assertFalse(fixture.server.reservations)
            refreshed = replace(request, profile=replace(request.profile,
                peak=records.ExpectedPeak(12 * GIB, "measured", 9), estimated_at=fixture.clock.now()))
            _ = fixture.server.handle_request(refreshed)
            saved = fixture.server.waiters[request.episode].row
            self.assertEqual((saved["since"], saved["need"], saved.get("need_runs")), (request.since, 12 * GIB, 9))
            self.assertNotIsInstance(fixture.server.waiters[request.episode].last_reply, service.NoWaitReply)

    def test_policy_changes_reclassify_new_passes_without_changing_an_earlier_reply(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=12 * GIB)
            grant = only_admitted(fixture.server.handle_request(request))
            record = fixture.server.reservations[request.episode]
            fixture.hold_starts()
            younger = fixture.request(need=10 * GIB)
            _ = fixture.server.handle_request(younger)
            old, = fixture.server.handle_request(wire.ExplainRequest())
            assert isinstance(old, wire.ExplainReply)
            self.assertIsInstance(old.explanation.host, records.HostAvailable)
            assert isinstance(old.explanation.host, records.HostAvailable)
            self.assertIsInstance(old.explanation.host.pressure, records.MemoryPressure)
            old_release = gate.smallest_head_release(old.explanation)
            fixture.configure(full_pressure_limit=100)
            _ = fixture.server.run_pass()
            current, = fixture.server.handle_request(wire.ExplainRequest())
            assert isinstance(current, wire.ExplainReply)
            assert isinstance(current.explanation.host, records.HostAvailable)
            self.assertIsInstance(current.explanation.host.pressure, records.NoPressureHold)
            self.assertEqual(old.explanation.policy.pressure.full_avg10_limit, 5)
            self.assertEqual(gate.smallest_head_release(old.explanation), old_release)
            self.assertEqual(fixture.server.reservations[request.episode].reservation_id, grant.reservation)
            self.assertEqual(record.need, 12 * GIB)


    def test_filtered_estimate_is_reused_and_release_keeps_repository_and_selection(self) -> None:
        with AdmissionFixture() as fixture:
            argv = ["cargo", "nextest", "run", "--test-threads", "16", "-E", "package(hana) & test(native_rate)"]
            repository = records.RepositoryIdentity("https://example.org/hana", "hana")
            peak = records.ExpectedPeak(2 * GIB, "measured", 5)
            policy = records.read_memory_config().policy
            from datetime import UTC, datetime
            with mock.patch.object(step_estimator, "git_identity", return_value=(repository, "clone")) as identity, \
                 mock.patch.object(step_estimator, "expected_peak", return_value=peak) as history:
                first = step_estimator.step_estimate(argv, str(fixture.root), records.InferFromArgv(),
                    datetime.fromtimestamp(fixture.clock.now(), UTC), fixture.state_dir.parent, policy, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
                second = step_estimator.step_estimate(argv, str(fixture.root), first.profile,
                    datetime.fromtimestamp(fixture.clock.now() + 1, UTC), fixture.state_dir.parent, policy, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
            self.assertEqual((identity.call_count, history.call_count), (1, 1))
            self.assertEqual(history.call_args.kwargs["package"], "hana")
            self.assertEqual(history.call_args.kwargs["filters"], ("native_rate",))
            self.assertEqual(second.profile.peak, first.profile.peak)
            request = fixture.request(need=2 * GIB, width=records.RequestedTestWidth(16))
            request = replace(request, argv=tuple(argv), profile=second.profile,
                sidecar=str(fixture.scope(request.owner)))
            fixture.free(0)
            fixture.hold_starts()
            held = fixture.server.handle_request(request)[0]
            self.assertIsInstance(held, wire.Wait)
            fixture.free(40 * GIB)
            _ = fixture.server.run_pass()
            grant = fixture.server.ledger[request.episode].reply
            assert isinstance(grant, wire.Admitted)
            fixture.scoped_peak(grant, GIB)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("filtered", grant.reservation, 0)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual((row["repo_identity"], row["test_scope"]), (repository.key, "filtered"))
            self.assertEqual((row["package"], row["filters"], row["test_threads"]), ("hana", ["native_rate"], 16))


    def test_one_process_snapshot_covers_fresh_mature_and_reused_owners(self) -> None:
        with AdmissionFixture() as fixture:
            fresh = fixture.request(need=4 * GIB, owner=fixture.proc.spawn(GIB))
            fresh = replace(fresh, sidecar=str(fixture.scope(fresh.owner)))
            _ = only_admitted(fixture.server.handle_request(fresh))
            fixture.clock.advance(120)
            mature = fresh
            fresh = fixture.request(need=4 * GIB, owner=fixture.proc.spawn(GIB // 2))
            if fresh.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            fresh = replace(fresh, sidecar=str(fixture.scope(fresh.owner)))
            _ = only_admitted(fixture.server.handle_request(fresh))
            second_scope = Path(fresh.sidecar).read_text().strip()
            _ = (Path(second_scope) / "memory.current").write_text(str(GIB // 2))
            _ = (Path(second_scope) / "memory.stat").write_text(f"anon {GIB // 2}\nshmem 0\ninactive_file 0\n")
            reused = fixture.request(need=GIB)
            if reused.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            old = only_admitted(fixture.server.handle_request(reused))
            fixture.proc.fixture_owners[reused.owner.pid] = "reused-start"
            stat_path = fixture.proc.path / str(reused.owner.pid) / "stat"
            _ = stat_path.write_text(stat_path.read_text().replace(reused.owner.start_time, "reused-start"))
            with mock.patch.object(fixture.proc, "processes", wraps=fixture.proc.processes) as tables:
                result = fixture.server.run_pass()
            tables.assert_called_once_with()
            assert isinstance(result, gate.PassResult)
            self.assertEqual((result.policy.totals.promised, result.policy.totals.measured), (13 * GIB // 2, 3 * GIB // 2))
            self.assertEqual({record.owner.pid for record in fixture.server.reservations.values()},
                {fresh.owner.pid, mature.owner.pid})
            self.assertFalse(Path(old.max_path).exists())


    def test_state_write_interruption_keeps_the_saved_width_for_restart_and_retry(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(width=records.RequestedTestWidth(8))
            grant = only_admitted(fixture.server.handle_request(request))
            before = fixture.state_text()
            mutation = wire.WidthRequest("new-width", grant.reservation, 2)
            replace_file = os.replace
            def stop(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
                if Path(destination) == fixture.state_dir / "state.json":
                    raise OSError("state replace stopped")
                replace_file(source, destination)
            with mock.patch.object(os, "replace", side_effect=stop):
                with self.assertRaisesRegex(OSError, "state replace stopped"):
                    _ = fixture.server.handle_request(mutation)
            self.assertEqual(fixture.state_text(), before)
            fixture.restart()
            self.assertEqual(fixture.server.reservations[request.episode].test_width, records.RequestedTestWidth(8))
            self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
            fixture.restart()
            self.assertEqual(fixture.server.reservations[request.episode].test_width, records.RequestedTestWidth(2))


class RunPlaceTests(unittest.TestCase):
    def reading(self, fixture: AdmissionFixture) -> records.QueueRead:
        reply, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        return reply.reading

    def test_phase_ten_client_reconnect_after_restart_keeps_place_and_replays_one_grant(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request()
            envelope = wire.parse_record(wire.encode_request(request), dict[str, object])
            data = cast(dict[str, object], envelope["data"])
            _ = data.pop("run", None)
            legacy = wire.encode_record(envelope) + "\n"
            peer = fixture.connect(legacy)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            peer.close()
            fixture.restart()
            fixture.clock.advance(1)
            other = fixture.request()
            _ = fixture.server.handle_request(other)
            peer = fixture.connect(legacy)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            self.assertEqual(fixture.queue_pids(), [request.owner.pid, other.owner.pid])
            self.assertEqual(fixture.server.waiters[request.episode].request, request)
            self.assertFalse(service.read_state(fixture.state_dir).run_places)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            granted = only_admitted(peer.replies())
            peer.close()
            fixture.restart()
            replay = fixture.connect(legacy)
            self.assertEqual(only_admitted(replay.replies()), granted)
            self.assertEqual(len([record for record in fixture.server.reservations.values()
                                  if record.owner == request.owner]), 1)

    def test_run_order_never_moves_an_earlier_step_behind_its_own_arrival(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.free(0)
            fixture.hold_starts()
            peer = fixture.request(since=first.since - 10)
            _ = fixture.server.handle_request(peer)
            earlier = fixture.request(run=run, since=first.since - 20)
            _ = fixture.server.handle_request(earlier)
            self.assertEqual(fixture.queue_pids(), [earlier.owner.pid, peer.owner.pid])
            row, = (row for _, row in self.reading(fixture).ordered if row["pid"] == earlier.owner.pid)
            self.assertEqual(row["since"], earlier.since)
            assert "order_since" in row
            self.assertEqual(row["order_since"], earlier.since)

    def test_later_child_keeps_run_order_and_run_exit_restores_step_arrival(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(10)
            peers = [fixture.request()]
            _ = fixture.server.handle_request(peers[0])
            fixture.clock.advance(10)
            peers.append(fixture.request())
            _ = fixture.server.handle_request(peers[1])
            fixture.clock.advance(10)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, *(peer.owner.pid for peer in peers)])
            row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == second.owner.pid)
            self.assertEqual(row["since"], second.since)
            assert "order_since" in row
            self.assertEqual(row["order_since"], first.since)
            place = next(place for place in service.read_state(fixture.state_dir).places.values()
                         if place.owner == second.owner)
            self.assertEqual(place.since, second.since)
            self.assertNotEqual(first.owner, second.owner)
            fixture.end_run(run)
            self.assertEqual(fixture.queue_pids(), [*(peer.owner.pid for peer in peers), second.owner.pid])
            self.assertEqual(fixture.server.waiters[second.episode].row["since"], second.since)
            state = service.read_state(fixture.state_dir)
            self.assertFalse(state.run_places)
            restored = next(place for place in state.places.values() if place.owner == second.owner)
            self.assertEqual(restored.since, second.since)
            row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == second.owner.pid)
            assert "order_since" in row
            self.assertEqual(row["order_since"], second.since)
            self.assertFalse(fixture.server.reservations)

    def test_restart_between_steps_keeps_the_run_watch_without_a_memory_charge(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            fixture.clock.advance(2)
            state = service.read_state(fixture.state_dir)
            self.assertEqual(list(state.run_places.values()), [wire.RunPlace(run, first.since, first.owner)])
            self.assertFalse(state.reservations)
            self.assertFalse(state.places)
            fixture.restart()
            fixture.free(0)
            fixture.hold_starts()
            other = fixture.request()
            _ = fixture.server.handle_request(other)
            fixture.clock.advance(2)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, other.owner.pid])
            self.assertEqual(fixture.server.waiters[second.episode].row["since"], second.since)
            row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == second.owner.pid)
            assert "order_since" in row
            self.assertEqual(row["order_since"], first.since)
            fixture.end_run(run)
            self.assertEqual(fixture.queue_pids(), [other.owner.pid, second.owner.pid])
            self.assertFalse(service.read_state(fixture.state_dir).run_places)

    def test_run_exit_between_steps_removes_the_idle_watch_and_saved_place(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            self.assertTrue(service.read_state(fixture.state_dir).run_places)
            fixture.end_run(run)
            self.assertFalse(service.read_state(fixture.state_dir).run_places)
            self.assertFalse(fixture.server.reservations)
            fixture.clock.advance(1)
            fixture.free(0)
            fixture.hold_starts()
            stale = fixture.request(run=run)
            _ = fixture.server.handle_request(stale)
            row, = (row for _, row in self.reading(fixture).ordered)
            assert "order_since" in row
            self.assertEqual(row["order_since"], stale.since)

    def test_child_episodes_and_reservations_stay_separate_under_one_run(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            fixture.free(0)
            fixture.hold_starts()
            first = fixture.request(run=run)
            _ = fixture.server.handle_request(first)
            fixture.clock.advance(1)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [first.owner.pid, second.owner.pid])
            for _, row in self.reading(fixture).ordered:
                assert "order_since" in row
                self.assertEqual(row["order_since"], first.since)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            one = only_admitted([fixture.server.ledger[first.episode].reply])
            fixture.allow_next_start()
            _ = fixture.server.run_pass()
            two = only_admitted([fixture.server.ledger[second.episode].reply])
            self.assertNotEqual(first.episode, second.episode)
            self.assertNotEqual(one.reservation, two.reservation)
            self.assertEqual(set(fixture.server.reservations), {first.episode, second.episode})
            for request, granted in ((first, one), (second, two)):
                record = fixture.server.reservations[request.episode]
                self.assertEqual(record.owner, request.owner)
                self.assertEqual(record.queue_waiter["since"], request.since)
                replay = only_admitted(fixture.server.handle_request(request))
                self.assertEqual(replay, granted)
            self.assertEqual(len(service.read_state(fixture.state_dir).run_places), 1)
            _ = fixture.server.handle_request(wire.ReleaseRequest("release-one", one.reservation, 0))
            self.assertEqual(set(fixture.server.reservations), {second.episode})
            _ = fixture.server.handle_request(wire.ReleaseRequest("release-two", two.reservation, 0))
            self.assertFalse(fixture.server.reservations)
            self.assertEqual(len(service.read_state(fixture.state_dir).run_places), 1)

    def test_reconnect_and_token_reacquire_use_the_child_arrival_key(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.clock.advance(10)
            second = fixture.request(run=run)
            two = only_admitted(fixture.server.handle_request(second))
            _ = fixture.server.handle_request(wire.TokenWaitRequest("token-gap", two.reservation))
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(11)
            again = replace(second, episode="child-after-token", token_back_at=fixture.clock.now())
            _ = fixture.server.handle_request(again)
            fixture.restart()
            _ = fixture.server.handle_request(again)
            _ = fixture.server.handle_request(again)
            places = [place for place in service.read_state(fixture.state_dir).places.values()
                      if place.owner == second.owner]
            self.assertEqual(len(places), 1)
            self.assertEqual(places[0].since, second.since)
            row, = (row for _, row in self.reading(fixture).ordered)
            self.assertEqual(row["since"], second.since)
            assert "order_since" in row
            self.assertEqual(row["order_since"], first.since)
            with (fixture.state_dir / "queue_times.jsonl").open("rb") as stream:
                gaps = [row for row in gate.queue_times_backward(stream) if row.get("kind") == "token_gap"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["episode"], again.episode)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            later = only_admitted([fixture.server.ledger[again.episode].reply])
            self.assertNotEqual(later.reservation, two.reservation)

    def test_reused_run_pid_and_no_run_keep_each_steps_own_order(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(1)
            ordinary = fixture.request()
            _ = fixture.server.handle_request(ordinary)
            requests: list[wire.AdmitRequest] = []
            for identity in (wire.RunIdentity(run.pid, str(int(run.start_time) + 1)), wire.NoRun()):
                fixture.clock.advance(1)
                request = fixture.request(run=identity)
                requests.append(request)
                _ = fixture.server.handle_request(request)
                row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == request.owner.pid)
                assert "order_since" in row
                self.assertEqual(row["order_since"], request.since)
            self.assertEqual(fixture.queue_pids(), [ordinary.owner.pid, *(request.owner.pid for request in requests)])
            self.assertEqual(list(service.read_state(fixture.state_dir).run_places.values()),
                             [wire.RunPlace(run, first.since, first.owner)])

    def test_restart_drops_a_run_that_ended_while_the_service_was_stopped(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.server.handle_request(first)
            fixture.clock.advance(1)
            peer = fixture.request()
            _ = fixture.server.handle_request(peer)
            fixture.clock.advance(1)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            fixture.server.close()
            fixture.proc.kill(wire.Owner(run.pid, run.start_time))
            fixture.server = fixture.new_server()
            for request in (second, peer, first):
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.queue_pids(), [first.owner.pid, peer.owner.pid, second.owner.pid])
            self.assertFalse(service.read_state(fixture.state_dir).run_places)

    def test_run_types_round_trip_in_admit_and_service_state(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            run = fixture.run_owner()
            request = fixture.request(run=run)
            for identity in (run, wire.NoRun()):
                with self.subTest(identity=identity):
                    message = replace(request, run=identity)
                    self.assertEqual(wire.parse_request(wire.encode_request(message)), message)
            _ = fixture.server.handle_request(request)
            state = service.read_state(fixture.state_dir)
            self.assertEqual(list(state.run_places.values()), [wire.RunPlace(run, request.since, request.owner)])
            self.assertEqual(wire.parse_record(wire.encode_record(state), service.ServiceState), state)
            for value, schema in ((run, wire.RunIdentity), (wire.NoRun(), wire.NoRun),
                                  (wire.RunPlace(run, request.since, request.owner), wire.RunPlace)):
                self.assertEqual(codec.parse_snapshot_value(codec.snapshot_value(value), schema), value)
            fixture.restart()
            self.assertEqual(service.read_state(fixture.state_dir), state)

    def test_phase_ten_state_upgrade_preserves_pending_work_priority_and_replay(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("pending-release", grant.reservation, 137))
            saved = service.read_state(fixture.state_dir)
            self.assertNotIsInstance(saved.reservations[request.episode].finalization, service.NoFinalizationIntent)
            priority = records.QueuePriority(({"unit": "queue", "why": "the user", "set_at": fixture.clock.now(),
                "set_by": "director"},))
            expected = replace(saved, priority=priority, build_history_generation=7)
            old = wire.parse_record(wire.encode_record(expected), dict[str, object])
            del old["run_places"]
            path = fixture.state_dir / "state.json"
            _ = path.write_text(wire.encode_record(old))
            before = path.read_text()
            restored = service.read_state(fixture.state_dir)
            self.assertEqual(restored, expected)
            self.assertEqual(path.read_text(), before)
            self.assertEqual(restored.ledger[request.episode].reply, grant)
            self.assertEqual(restored.reservations[request.episode].ending, saved.reservations[request.episode].ending)

    def test_display_age_and_aws_wait_threshold_use_child_arrival_not_run_order(self) -> None:
        from zoneinfo import ZoneInfo
        from scripts.build_queue import head_facts
        from scripts.build_queue.queue_lines import plain

        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, since=fixture.clock.now() - 600)
            _ = fixture.finish_step(first)
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(11)
            second = fixture.request(run=run, step="check")
            second = replace(second, profile=replace(second.profile,
                repository=records.RepositoryIdentity("github.com/team/project", "project")))
            _ = fixture.server.handle_request(second)
            reading = self.reading(fixture)
            self.assertEqual(reading.view.waiting[0].since, second.since)
            model = queue_view.QueueModel(reading, {}, records.NoWaiter(), head_facts.PaceUnavailable(), ZoneInfo("UTC"),
                queue_view.HostLoadUnavailable(), gate.smallest_head_release(reading.explanation))
            rendered = plain(queue_view.render_view(model, 100))
            heading, = [line for line in rendered.splitlines() if line.lstrip().startswith("# ")]
            row, = [line for line in rendered.splitlines()
                    if line.lstrip().startswith("1 ") and "project check" in line]
            minute_column = heading.index("min")
            self.assertEqual(row[minute_column:minute_column + 3].strip(), "0", rendered)


class AdmissionConfigurationTests(unittest.TestCase):
    def test_slow_requests_log_wall_cpu_and_kind_but_fast_requests_do_not(self) -> None:
        for slow_configuration in (False, True):
            with self.subTest(slow_configuration=slow_configuration), AdmissionFixture() as fixture:
                write_memory_config(fixture.root / "build_admission.conf", request_slow_seconds="0.25")
                durations = iter((0.5, 0.1))
                read = conf.read

                def measured(request: wire.Request, connection: service.Connection | service.NoConnection) -> list[wire.Reply]:
                    del request, connection
                    if not slow_configuration:
                        fixture.clock.advance(next(durations))
                    return [wire.BuildHistoryReply(0)]

                def configuration(stem: str) -> conf.ConfigRead:
                    if slow_configuration:
                        fixture.clock.advance(next(durations))
                    return read(stem)

                output = io.StringIO()
                with mock.patch.object(fixture.server, "_handle_request", side_effect=measured), \
                        mock.patch.object(conf, "read", side_effect=configuration), \
                        mock.patch.object(time, "thread_time", side_effect=(2.0, 2.125, 3.0, 3.01)), \
                        redirect_stderr(output):
                    for _ in range(2):
                        self.assertEqual(fixture.server.handle_request(wire.BuildHistoryRequest()), [wire.BuildHistoryReply(0)])
                self.assertEqual(output.getvalue(), "admission request BuildHistoryRequest: wall=0.500s cpu=0.125s\n")

    def test_bad_request_timing_limit_logs_once_and_keeps_requests_and_cleanup_available(self) -> None:
        with AdmissionFixture() as fixture:
            grant = only_admitted(fixture.server.handle_request(fixture.request()))
            write_memory_config(fixture.root / "build_admission.conf", request_slow_seconds="invalid")
            output = io.StringIO()
            with mock.patch.object(records, "_warned_configuration_diagnostics", set[records.ConfigurationDiagnostic]()), \
                    redirect_stderr(output):
                for _ in range(2):
                    self.assertIsInstance(fixture.connect(wire.BuildHistoryRequest()).replies()[0], wire.BuildHistoryReply)
                fixture.release(grant)
            self.assertEqual(output.getvalue().count("request_slow_seconds"), 1)
            self.assertFalse(fixture.server.reservations)


    def test_ledger_grants_restore_replay_and_release_without_usable_configuration(self) -> None:
        with AdmissionFixture() as fixture:
            old_request = fixture.request(need=4 * GIB)
            old_grant = only_admitted(fixture.server.handle_request(old_request))
            saved_request = fixture.request(need=4 * GIB)
            if saved_request.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            saved_grant = only_admitted(fixture.server.handle_request(saved_request))
            saved_grant = replace(saved_grant, cleanup_timing=wire.CleanupTiming(17, 3))
            fixture.server.ledger[saved_request.episode] = replace(
                fixture.server.ledger[saved_request.episode], reply=saved_grant)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("absent", "absent", 0)), [wire.Ok()])
            fixture.server.close()
            write_memory_config(fixture.root / "build_admission.conf", estimate_refresh_seconds="invalid")
            fixture.server = fixture.new_server()
            expected = old_grant
            if old_request.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            self.assertEqual(only_admitted(fixture.server.handle_request(old_request)), expected)
            if saved_request.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            self.assertEqual(only_admitted(fixture.server.handle_request(saved_request)), saved_grant)
            self.assertEqual(fixture.server.ledger["absent"].reply, wire.Ok())
            with mock.patch.object(fixture.server, "_log_request_timing"), \
                    mock.patch.object(conf, "read", side_effect=AssertionError("release read config")):
                for grant in (expected, saved_grant):
                    self.assertEqual(fixture.server.handle_request(
                        wire.ReleaseRequest(f"release-{grant.reservation}", grant.reservation, 0)), [wire.Ok()])
            self.assertFalse(service.read_state(fixture.state_dir).reservations)

    def test_watch_errors_keep_cleanup_available_and_retry_configuration_recovery(self) -> None:
        for operation in ("is_dir", "add_watch"):
            with self.subTest(operation=operation), AdmissionFixture() as fixture:
                grant = only_admitted(fixture.server.handle_request(fixture.request()))
                folder = fixture.root / "watched-config"
                config = folder / "build_admission.conf"
                write_memory_config(config, estimate_refresh_seconds="invalid")
                with mock.patch.dict(os.environ, {"CONF_DIR": str(folder)}):
                    fixture.restart()
                    config.unlink()
                    folder.rmdir()
                    is_dir = Path.is_dir
                    ancestors = {folder, *folder.parents}

                    def inaccessible(path: Path) -> bool:
                        if path in ancestors:
                            raise OSError(errno.EACCES, "watch folder unavailable")
                        return is_dir(path)

                    patch = (mock.patch.object(Path, "is_dir", inaccessible) if operation == "is_dir" else
                             mock.patch.object(inotify, "add_watch", side_effect=OSError(errno.EACCES, "watch denied")))
                    with patch:
                        fixture.server.run_once(0)
                        self.assertFalse(fixture.server.stopped)
                        self.assertEqual(fixture.server.config_watch, -1)
                        self.assertTrue(math.isfinite(fixture.server.next_pass))
                        self.assertEqual(fixture.connect(wire.ReleaseRequest("release", grant.reservation, 0)).replies(),
                                         [wire.Ok()])
                        self.assertFalse(fixture.server.reservations)
                    write_memory_config(config)
                    fixture.clock.advance(wire.PROTOCOL_CLEANUP_TIMING.reconnect_seconds)
                    fixture.server.run_once(0)
                    self.assertIsInstance(fixture.server.last_policy, records.AdmissionPolicy)
                    self.assertIsInstance(fixture.connect(wire.ViewRequest()).replies()[0], wire.ViewReply)
                    fixture.allow_next_start()
                    request = fixture.request()
                    self.assertEqual(only_admitted(fixture.server.handle_request(request)).need, request.profile.peak.bytes)

    def test_persist_prunes_dead_and_reused_callers_but_keeps_live_callers_without_reservations(self) -> None:
        with AdmissionFixture() as fixture:
            dead, reused, live = (fixture.request() for _ in range(3))
            for request in (dead, reused, live):
                _ = fixture.finish_step(request)
            fixture.proc.kill(dead.owner)
            owner_start = fixture.proc.owner_start

            def current_start(pid: int) -> service.LiveOwnerIdentity | service.OwnerProcessGone:
                return service.LiveOwnerIdentity(reused.owner.start_time + "1") if pid == reused.owner.pid else owner_start(pid)

            write_memory_config(fixture.root / "build_admission.conf", estimate_refresh_seconds="invalid")
            _ = fixture.server.run_pass()
            with mock.patch.object(fixture.proc, "owner_start", side_effect=current_start):
                fixture.server.persist()
            self.assertFalse(fixture.server.reservations)
            self.assertEqual(service.read_state(fixture.state_dir).caller_versions,
                             {f"{live.owner.pid}-{live.owner.start_time}": live.code_version})

    def test_release_keeps_unrelated_running_evidence_and_defers_admission_until_query(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.compiler(GIB)
            scope = fixture.root / "builds.slice/cache.scope"
            scope.mkdir(parents=True)
            _ = (scope / "memory.current").write_text(str(GIB))
            _ = (scope / "memory.stat").write_text("inactive_file 0\n")
            _ = (scope / "cgroup.procs").write_text("")
            first = fixture.request(need=4 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            second = fixture.request(need=4 * GIB)
            if second.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            _ = only_admitted(fixture.server.handle_request(second))
            waiting = fixture.request(need=14 * GIB)
            self.assertIsInstance(fixture.server.handle_request(waiting)[0], wire.Wait)
            before = fixture.server.last_inputs
            assert isinstance(before, records.QueueInputs)
            other_running = tuple(step for step in before.running if step.session_id != first.identity.session_id)
            with mock.patch.object(gate, "admission_pass", side_effect=AssertionError("release ran admission")):
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0)), [wire.Ok()])
            after = fixture.server.last_inputs
            assert isinstance(after, records.QueueInputs)
            self.assertEqual(after.running, other_running)
            self.assertEqual({row["pid"] for row in after.reservations}, {second.owner.pid})
            self.assertNotIn(waiting.episode, fixture.server.reservations)
            fixture.allow_next_start()
            with mock.patch.object(gate, "admission_pass", wraps=gate.admission_pass) as passed:
                self.assertIsInstance(fixture.server.handle_request(wire.ViewRequest())[0], wire.ViewReply)
            self.assertEqual(passed.call_count, 1)
            self.assertNotIn(waiting.episode, fixture.server.reservations)
            fixture.server.run_once(0)
            self.assertIn(waiting.episode, fixture.server.reservations)

    def test_drain_ignores_current_socket_waiters_and_counts_older_caller_code(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        for relative in ("direct/verify.sh", "lint/invoke.sh", "production/merge_gate.py", "build_queue/admission_client.py",
                         "mac_test/offload.py", "mac_test/copy_watch.py"):
            with self.subTest(script=relative), AdmissionFixture() as fixture:
                caller = fixture.proc.spawn()
                script = fixture.root / Path(relative).name
                current = (scripts / relative).read_bytes()
                _ = script.write_bytes(current)
                _ = (fixture.proc.path / str(caller.pid) / "cmdline").write_bytes(
                    f"python3\0{script}\0".encode())
                request = replace(fixture.request(owner=caller), code_version=wire.admission_code_version())
                grant = only_admitted(fixture.server.handle_request(request))
                _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
                for older in (False, True):
                    with self.subTest(older=older), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                        _ = script.write_bytes(current + b"\n# older caller snapshot\n" if older else current)
                        status = service.drain_check(fixture.state_dir, service=True,
                                                     include_gate_callers=True, proc=fixture.proc)
                    self.assertEqual(status, 1 if older else 0)
                    if older:
                        self.assertIn(f"clients: 1: {caller.pid}", output.getvalue())
                    else:
                        self.assertEqual(output.getvalue(), "drained\n")

    def test_drain_counts_a_caller_with_an_older_imported_admission_module(self) -> None:
        with AdmissionFixture() as fixture:
            scripts = fixture.root / "scripts"
            source = Path(__file__).resolve().parents[1]
            for relative in admission_code.ADMISSION_CODE_PATHS:
                target = scripts / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _ = target.write_bytes((source / relative).read_bytes())
            caller = fixture.proc.spawn()
            script = scripts / "direct/verify.sh"
            _ = (fixture.proc.path / str(caller.pid) / "cmdline").write_bytes(
                f"bash\0{script}\0".encode())
            loaded_version = wire.admission_code_version(scripts)
            request = replace(fixture.request(owner=caller), code_version=loaded_version)
            grant = only_admitted(fixture.server.handle_request(request))
            _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
            with mock.patch.object(service, "__file__", str(scripts / "build_queue/admission_service.py")):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(service.drain_check(fixture.state_dir, service=True,
                        include_gate_callers=True, proc=fixture.proc), 0)
                self.assertEqual(output.getvalue(), "drained\n")
                imported = scripts / "lint/memory_admit.py"
                _ = imported.write_bytes(imported.read_bytes() + b"\n# Changed admission code.\n")
                with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(service.drain_check(fixture.state_dir, service=True,
                        include_gate_callers=True, proc=fixture.proc), 1)
                self.assertIn(f"clients: 1: {caller.pid}", output.getvalue())

    def test_drain_counts_a_caller_without_a_recorded_admission_version(self) -> None:
        with AdmissionFixture() as fixture:
            caller = fixture.proc.spawn()
            script = Path(__file__).resolve().parents[1] / "direct/verify.sh"
            _ = (fixture.proc.path / str(caller.pid) / "cmdline").write_bytes(
                f"bash\0{script}\0".encode())
            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(service.drain_check(fixture.state_dir, service=True,
                    include_gate_callers=True, proc=fixture.proc), 1)
            self.assertIn(f"clients: 1: {caller.pid}", output.getvalue())

    def test_unusable_configuration_bounds_requests_and_keeps_the_last_usable_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "build_admission.conf"
            write_memory_config(config, request_max_bytes="invalid")
            with AdmissionFixture(root=root) as fixture:
                oversized = fixture.connect("x" * (wire.CLEANUP_REQUEST_MAX_BYTES + 1))
                refused, = oversized.replies()
                self.assertIsInstance(refused, wire.Refused)
                self.assertIn(str(config), wire.encode_reply(refused))
                self.assertIn("request_max_bytes", wire.encode_reply(refused))
                self.assertFalse(fixture.server.reservations)
                maximum_cleanup = wire.ReleaseRequest("\0" * wire.CLEANUP_STRING_MAX,
                    "\0" * wire.CLEANUP_STRING_MAX, wire.CLEANUP_STATUS_MIN,
                    code_version="\0" * wire.CLEANUP_STRING_MAX)
                self.assertEqual(fixture.connect(maximum_cleanup).replies(), [wire.Ok()])
                write_memory_config(config, request_max_bytes="512")
                fixture.server.run_once(0)
                request = fixture.request()
                grant = only_admitted(fixture.server.handle_request(request))
                write_memory_config(config, request_max_bytes="invalid")
                fixture.server.run_once(0)
                large = replace(request, argv=("cargo", "x" * 1024))
                refused, = fixture.connect(large).replies()
                self.assertIsInstance(refused, wire.Refused)
                self.assertIn("exceeds", wire.encode_reply(refused))
                cleanup = wire.ReleaseRequest("r" * wire.CLEANUP_STRING_MAX, grant.reservation, 0)
                self.assertGreater(len(wire.encode_request(cleanup).encode()), 512)
                self.assertEqual(fixture.connect(cleanup).replies(), [wire.Ok()])
                self.assertFalse(fixture.server.reservations)

    def test_missing_config_folder_rearms_its_watch_before_the_file_arrives(self) -> None:
        with AdmissionFixture() as fixture:
            folder = fixture.root / "new-config"
            config = folder / "build_admission.conf"
            with mock.patch.dict(os.environ, {"CONF_DIR": str(folder)}):
                fixture.restart()
                request = fixture.request()
                refused, = fixture.server.handle_request(request)
                self.assertIsInstance(refused, wire.Refused)
                self.assertIn(str(config), wire.encode_reply(refused))
                folder.mkdir()
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.reservations)
                write_memory_config(config)
                fixture.server.run_once(0)
                view, = fixture.server.handle_request(wire.ViewRequest())
                self.assertIsInstance(view, wire.ViewReply)
                grant = only_admitted(fixture.server.handle_request(request))
                self.assertEqual(grant.need, request.profile.peak.bytes)
                self.assertEqual(len(fixture.server.reservations), 1)

    def test_config_permission_repair_recovers_without_an_inode_or_mtime_change(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            before = config.stat()
            config.chmod(0)
            try:
                fixture.server.run_once(0)
                request = fixture.request()
                refused, = fixture.server.handle_request(request)
                self.assertIsInstance(refused, wire.Refused)
                self.assertIn(str(config), wire.encode_reply(refused))
                self.assertFalse(fixture.server.reservations)
            finally:
                config.chmod(stat.S_IMODE(before.st_mode))
            self.assertEqual((config.stat().st_ino, config.stat().st_mtime_ns),
                             (before.st_ino, before.st_mtime_ns))
            fixture.server.run_once(0)
            view, = fixture.server.handle_request(wire.ViewRequest())
            self.assertIsInstance(view, wire.ViewReply)
            grant = only_admitted(fixture.server.handle_request(request))
            self.assertEqual(grant.need, request.profile.peak.bytes)
            self.assertEqual(len(fixture.server.reservations), 1)

    def test_memory_and_cloud_config_reads_supply_a_complete_service_pass(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            with mock.patch.object(conf, "read", wraps=conf.read) as reader:
                _ = fixture.server.run_pass()
            self.assertEqual(reader.call_args_list, [mock.call("build_admission"), mock.call("build_admission")])

    def test_unusable_config_restart_replays_one_existing_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            write_memory_config(fixture.root / "build_admission.conf", estimate_refresh_seconds="invalid")
            _ = fixture.server.run_pass()
            fixture.restart()
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    if request.episode not in fixture.server.reservations:
                        fixture.allow_next_start()
                    self.assertEqual(only_admitted(fixture.server.handle_request(request)), grant)
                    self.assertEqual(set(fixture.server.reservations), {request.episode})
                    record = fixture.server.reservations[request.episode]
                    self.assertEqual(record.owner, request.owner)
                    self.assertEqual(record.queue_waiter["since"], request.since)
            fixture.end_owner(request.owner)
            self.assertFalse(fixture.server.reservations)
            self.assertFalse(service.read_state(fixture.state_dir).reservations)

    def test_unusable_startup_serves_refusal_and_recovers_on_the_next_pass(self) -> None:
        for bad in (None, "invalid"):
            with self.subTest(value=bad), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "build_admission.conf"
                write_memory_config(config, estimate_refresh_seconds=bad)
                with AdmissionFixture(root=root) as fixture:
                    self.assertTrue(fixture.socket_path.is_socket())
                    request = fixture.request()
                    replies = fixture.server.handle_request(request)
                    self.assertFalse(any(isinstance(reply, wire.Admitted) for reply in replies))
                    self.assertFalse(fixture.server.reservations)
                    reply, = fixture.server.handle_request(wire.ViewRequest())
                    encoded = wire.encode_reply(reply)
                    self.assertIn(str(config), encoded)
                    self.assertIn("estimate_refresh_seconds", encoded)
                    self.assertEqual(wire.parse_reply(encoded), reply)
                    socket_reply, = fixture.connect(wire.ViewRequest()).replies()
                    self.assertEqual(socket_reply, reply)
                    self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("absent", "absent", 0)), [wire.Ok()])
                    write_memory_config(config, estimate_refresh_seconds="600")
                    _ = fixture.server.run_pass()
                    granted = only_admitted(fixture.server.handle_request(request))
                    self.assertEqual(granted.need, request.profile.peak.bytes)
                    self.assertEqual(len(fixture.server.reservations), 1)

    def test_bad_live_key_stops_grants_and_repair_keeps_the_waiter_identity(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.hold_starts()
            request = fixture.request()
            _ = fixture.server.handle_request(request)
            place, = service.read_state(fixture.state_dir).places.values()
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, estimate_refresh_seconds="invalid")
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            self.assertFalse(fixture.server.reservations)
            saved_place, = service.read_state(fixture.state_dir).places.values()
            self.assertEqual(saved_place, place)
            reply, = fixture.server.handle_request(wire.ViewRequest())
            self.assertIn("estimate_refresh_seconds", wire.encode_reply(reply))
            write_memory_config(config, estimate_refresh_seconds="900")
            _ = fixture.server.run_pass()
            granted = only_admitted(fixture.server.handle_request(request))
            self.assertEqual(granted.need, request.profile.peak.bytes)
            self.assertEqual(fixture.server.reservations[request.episode].owner, request.owner)
            self.assertEqual(fixture.server.reservations[request.episode].queue_waiter["since"], request.since)
            configuration = fixture.server.last_usable_configuration
            assert isinstance(configuration, records.MemoryConfig)
            self.assertEqual(configuration.policy.timing.estimate_refresh_seconds, 900)

    def test_committed_recovery_warns_once_and_grants_with_the_recovered_value(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, estimate_refresh_seconds="321")
            commit_config_fixture(config)
            write_memory_config(config, estimate_refresh_seconds="invalid")
            request = fixture.request()
            with mock.patch.object(records, "_warned_configuration_diagnostics", set[records.ConfigurationDiagnostic]()), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as errors:
                granted = only_admitted(fixture.server.handle_request(request))
                _ = fixture.server.run_pass()
            policy = fixture.server.last_policy
            assert isinstance(policy, records.AdmissionPolicy)
            self.assertEqual(policy.timing.estimate_refresh_seconds, 321)
            self.assertEqual(granted.need, request.profile.peak.bytes)
            self.assertEqual(errors.getvalue(), "")
            diagnostic, = fixture.server.last_diagnostics
            self.assertIn(str(config), diagnostic.diagnostic)
            self.assertIn("estimate_refresh_seconds='invalid'", diagnostic.diagnostic)

    def test_release_finishes_history_and_persists_without_configuration(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            granted = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(granted, 7 * GIB)
            progress.publish_counter(Path(request.sidecar), progress.ProgressCounter("build:1", 50, 100))
            _ = fixture.run_pass()
            self.assertTrue(fixture.server.progress_history.merged)
            write_memory_config(fixture.root / "build_admission.conf", replay_retention_seconds="invalid",
                                peak_history_share_percent="70")
            with mock.patch.object(fixture.server, "_log_request_timing"), \
                    mock.patch.object(conf, "read", side_effect=AssertionError("release read config")):
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release-bad-config", granted.reservation, 137)), [wire.Ok()])
                fixture.server.persist()
            self.assertNotIn(request.episode, fixture.server.reservations)
            self.assertFalse(service.read_state(fixture.state_dir).reservations)
            row, = fixture.history()
            self.assertEqual(row["anon_peak_bytes"], 7 * GIB)
            self.assertEqual(row["status"], 137)

    def test_restart_and_run_exit_preserve_step_arrivals_during_config_refusal(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(1)
            peer = fixture.request()
            _ = fixture.server.handle_request(peer)
            fixture.clock.advance(11)
            child = fixture.request(run=run)
            _ = fixture.server.handle_request(child)
            config = fixture.root / "build_admission.conf"
            self.assertEqual(fixture.queue_pids(), [child.owner.pid, peer.owner.pid])
            saved = service.read_state(fixture.state_dir)
            valid_configuration = config.read_text()
            write_memory_config(config, estimate_refresh_seconds="invalid")
            fixture.restart()
            restored = service.read_state(fixture.state_dir)
            self.assertEqual(restored.places, saved.places)
            self.assertEqual(restored.run_places, saved.run_places)
            _ = fixture.server.handle_request(child)
            _ = fixture.server.handle_request(peer)
            self.assertEqual(fixture.server.waiters[child.episode].row["since"], child.since)
            self.assertEqual(fixture.server.waiters[peer.episode].row["since"], peer.since)
            fixture.end_run(run)
            self.assertFalse(service.read_state(fixture.state_dir).run_places)
            _ = config.write_text(valid_configuration)
            _ = fixture.server.run_pass()
            self.assertEqual(fixture.queue_pids(), [peer.owner.pid, child.owner.pid])
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            fixture.allow_next_start()
            granted = only_admitted(fixture.server.handle_request(child))
            fixture.restart()
            if child.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            self.assertEqual(only_admitted(fixture.server.handle_request(child)), granted)
            self.assertEqual(len([record for record in fixture.server.reservations.values() if record.owner == child.owner]), 1)
