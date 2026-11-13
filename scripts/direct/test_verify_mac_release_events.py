"""Real verify.sh Mac release events with disposable tools."""
from __future__ import annotations

import unittest
import os
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.build_queue.admission_test_support import caller_admission
import ctypes
import time
import threading
from dataclasses import replace
from unittest import mock
from scripts.build_queue import admission_protocol as wire, placement
from scripts.build_queue.test_placement import local_candidate, placement_request
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacReleaseEventsTests(VerifyMacOffloadFixture):
    def test_local_release_before_routing_watcher_starts_needs_no_retry_wait(self) -> None:
        from scripts.direct import local_release_wait

        cycle = self.routing_fixture()
        call_id = "already-released"
        with mock.patch.dict(os.environ, self.environment), self.hold_environment():
            self.release_local_hold()
            with mock.patch.object(local_release_wait.LocalReleaseEvents, "wait",
                                   side_effect=AssertionError("an admitted call must not wait for another event")):
                local_release_wait.wait_for_local_release(3600, call_id)
        self.assert_no_pending_calls(cycle)
        admission = caller_admission(self, self.root, self.environment)
        for timing in ("before-arm", "after-wait"):
            with self.subTest(assignment=timing):
                admission.allow_next_start()
                occupied = admission.occupy_host()
                admission.free(0)
                request = placement_request(admission)
                request = replace(request, constraints=replace(request.constraints,
                    candidates=(local_candidate(admission),)))
                with admission.loop_turn():
                    _ = admission.server.handle_request(request)
                    destination = request.constraints.candidates[0].destination
                    admission.server.publish_host_evidence(placement.FreshHostEvidence(
                        admission.server.host_evidence_key(request, destination), admission.clock.now(), "",
                        placement.AllowedHost()))
                    _ = admission.server.run_pass()
                    self.assertNotIn(request.episode, admission.server.current_assignments)

                def assign() -> None:
                    with admission.loop_turn():
                        admission.free(60 * 1024 ** 3)
                        _ = admission.server.run_pass()

                with mock.patch.dict(os.environ, self.environment):
                    if timing == "before-arm":
                        assign()
                        with mock.patch.object(local_release_wait.LocalReleaseEvents, "wait",
                                side_effect=AssertionError("an existing assignment must wake before waiting")):
                            local_release_wait.wait_for_local_release(3600, call_id, request.episode,
                                                                     wait_for_assignment=True)
                    else:
                        waiting = threading.Event()
                        failures: list[BaseException] = []
                        real_wait = local_release_wait.LocalReleaseEvents.wait

                        def publish_assignment() -> None:
                            try:
                                if not waiting.wait(TEST_HANG_GUARD_SECS):
                                    raise AssertionError("the assignment waiter did not start")
                                assign()
                            except BaseException as error:
                                failures.append(error)

                        def observe_wait(waiter: local_release_wait.LocalReleaseEvents, seconds: float) -> None:
                            waiting.set()
                            real_wait(waiter, seconds)

                        publisher = threading.Thread(target=publish_assignment)
                        publisher.start()
                        try:
                            with mock.patch.object(local_release_wait.LocalReleaseEvents, "wait", autospec=True,
                                                   side_effect=observe_wait):
                                local_release_wait.wait_for_local_release(3600, call_id, request.episode,
                                                                         wait_for_assignment=True)
                        finally:
                            waiting.set()
                            publisher.join(TEST_HANG_GUARD_SECS)
                        self.assertFalse(publisher.is_alive())
                        self.assertEqual(failures, [])
                with admission.loop_turn():
                    self.assertIn(request.episode, admission.server.current_assignments)
                    granted = admission.server.ledger[request.episode].reply
                    self.assertIsInstance(granted, wire.Admitted)
                    assert isinstance(granted, wire.Admitted)
                    admission.release(granted)
                    admission.release(occupied)

        with self.subTest(assignment="consumed-remote"):
            request = placement_request(admission)
            destination = request.constraints.candidates[0].destination
            key = admission.server.host_evidence_key(request, destination)
            with admission.loop_turn():
                admission.server.publish_host_evidence(placement.FreshHostEvidence(
                    key, admission.clock.now(), "", placement.AllowedHost()))
                assigned, = admission.server.handle_request(request)
                self.assertIsInstance(assigned, wire.Assignment)
                assert isinstance(assigned, wire.Assignment)
                original = assigned.assignment
                claim = placement.ProviderClaimReference(destination.provider, destination.target, destination.scope,
                    "waiter-claim", original.episode, original.attempt)
                holder = placement.ClaimHolderIdentity(original.owner.host, original.owner.pid, original.owner.start_time)
                admission.server.publish_host_evidence(placement.FreshHostEvidence(key, admission.clock.now(), "",
                    placement.AllowedHost(), (placement.AdoptedProviderClaim(claim, holder, original.session,
                                                                            destination.host, ""),)))
                reply, = admission.server.handle_request(wire.ConsumePermission("waiter-consume", original, claim, holder))
                self.assertIsInstance(reply, wire.FirstAuthorization)
            replay_waiting = threading.Event()
            wait_calls: list[float] = []
            replay_failures: list[BaseException] = []
            new_attempts: list[str] = []
            replay_real_wait = local_release_wait.LocalReleaseEvents.wait

            def replace_consumed() -> None:
                try:
                    if not replay_waiting.wait(TEST_HANG_GUARD_SECS):
                        raise AssertionError("the consumed-assignment waiter did not start")
                    with admission.loop_turn():
                        admission.clock.advance(1)
                        admission.server.publish_host_evidence(placement.FreshHostEvidence(
                            key, admission.clock.now(), "", placement.AllowedHost()))
                        _ = admission.server.handle_request(wire.ExecutionReport("waiter-settle", original, claim,
                            placement.ExecutionNotStarted(), placement.PrelaunchRefused("busy"), placement.DispatchPathSettled()))
                        admission.clock.advance(1)
                        admission.server.publish_host_evidence(placement.FreshHostEvidence(
                            key, admission.clock.now(), "", placement.AllowedHost()))
                        replacement, = admission.server.handle_request(request)
                        if not isinstance(replacement, wire.Assignment):
                            raise AssertionError(f"the replacement was not assigned: {replacement}")
                        new_attempts.append(replacement.assignment.attempt)
                except BaseException as error:
                    replay_failures.append(error)

            def observe_consumed_wait(waiter: local_release_wait.LocalReleaseEvents, seconds: float) -> None:
                wait_calls.append(seconds)
                replay_waiting.set()
                replay_real_wait(waiter, seconds)

            publisher = threading.Thread(target=replace_consumed)
            publisher.start()
            try:
                with mock.patch.dict(os.environ, self.environment), \
                     mock.patch.object(local_release_wait.LocalReleaseEvents, "wait", autospec=True,
                                       side_effect=observe_consumed_wait):
                    local_release_wait.wait_for_local_release(3600, call_id, request.episode, wait_for_assignment=True)
            finally:
                replay_waiting.set()
                publisher.join(TEST_HANG_GUARD_SECS)
            self.assertFalse(publisher.is_alive())
            self.assertEqual(replay_failures, [])
            self.assertTrue(wait_calls, "a consumed remote assignment must not wake the waiter")
            with admission.loop_turn():
                current = admission.server.assignments[admission.server.current_assignments[request.episode]]
                self.assertEqual(new_attempts, [current.assignment.attempt])
                self.assertNotEqual(current.assignment.attempt, original.attempt)
                self.assertIsInstance(current.permission, placement.PermissionAvailable)
                self.assertEqual(admission.server.handle_request(wire.CancelPlacement(
                    "end-waiter-row", request.episode, request.owner, "the observation is complete")), [wire.Ok()])

    def test_routing_without_inotify_polls_until_local_release(self) -> None:
        from scripts.direct import local_release_wait

        cycle = self.routing_fixture()
        call_id = "polling-routing"
        def release_on_poll(_seconds: float) -> None:
            self.release_local_hold()

        with mock.patch.dict(os.environ, self.environment), self.hold_environment():
            with mock.patch.object(ctypes, "CDLL", return_value=object()), \
                 mock.patch.object(time, "monotonic", return_value=100), \
                 mock.patch.object(time, "sleep", side_effect=release_on_poll) as polling:
                local_release_wait.wait_for_local_release(15, call_id)
            polling.assert_called_once_with(15)
            self.assertFalse((Path(self.environment["BUILD_HOLD_DIR"]) / "hana").exists())
        self.assert_no_pending_calls(cycle)

    def test_free_mac_wins_when_local_hold_was_released_in_the_same_pass(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        admission = caller_admission(self, self.root, self.environment)
        with admission.loop_turn():
            self.release_local_hold()
            _ = (self.runner_state / "mac-free").write_text("free\n")
            for request in admission.server.placement_requests.values():
                if request.owner.pid != process.pid:
                    continue
                for candidate in request.constraints.candidates:
                    key = admission.server.host_evidence_key(request, candidate.destination)
                    admission.server.publish_host_evidence(admission.server.host_probe(key))
            _ = admission.server.run_pass()
            _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)

        self.assert_no_pending_calls(cycle)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "passed_filter")


if __name__ == "__main__":
    _ = unittest.main()
